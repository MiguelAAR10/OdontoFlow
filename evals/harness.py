"""Seeded, isolated databases and the in-process sandbox path for one trial.

One throwaway *template* database is migrated and seeded once per suite; every
trial runs on its own ``CREATE DATABASE … TEMPLATE`` clone plus its own agent
memory database, so repetitions cannot contaminate each other. All of them are
dropped on exit. The conversation goes through the same boundaries the sandbox
channel uses — canonical ingress, ``/sales-agent/turn``, ``/agent-tools/call``
and canonical outbound — wired in-process with ``TestClient``.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.config import get_settings
from evals.checks import World, snapshot
from evals.scenarios import SANDBOX_CHANNEL

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
DATABASE_PREFIX = "odontoflow_eval_"
BACKEND_BASE_URL = "http://127.0.0.1:8000"
ORGANIZATION_ID = 1
DEFAULT_PHONE = "+51987000111"


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


@contextmanager
def _preserved_logging() -> Iterator[None]:
    """Restore the root logger after ``alembic/env.py``'s ``fileConfig``.

    ``fileConfig`` replaces the root handlers and level, which would drop
    whatever was capturing logs (pytest's ``caplog`` among them) for the rest of
    the caller's process.
    """
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        yield
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers


@dataclass(frozen=True)
class Credentials:
    inbound_token: str
    agent_token: str


class EvalDatabase:
    """A migrated + seeded template database and its per-trial clones."""

    def __init__(self, *, anchor: date, server_url: str | None = None) -> None:
        base = make_url(server_url or get_settings().test_database_url)
        self.anchor = anchor
        self._base = base
        self._admin = base.set(database="odontoflow")
        self.template_name = f"{DATABASE_PREFIX}{os.getpid()}_{uuid4().hex[:8]}"
        self._created: list[str] = []
        self._sequence = 0
        self.credentials: Credentials | None = None

    def url(self, database: str) -> str:
        return _render(self._base.set(database=database))

    def _admin_execute(self, statement: str) -> None:
        engine = create_engine(self._admin, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                connection.execute(text(statement))
        finally:
            engine.dispose()

    def _create(self, name: str, *, template: str | None = None) -> str:
        suffix = f' TEMPLATE "{template}"' if template else ""
        self._admin_execute(f'CREATE DATABASE "{name}"{suffix}')
        self._created.append(name)
        return self.url(name)

    def _drop(self, name: str) -> None:
        self._admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        if name in self._created:
            self._created.remove(name)

    def __enter__(self) -> "EvalDatabase":
        try:
            url = self._create(self.template_name)
            config = Config(str(ALEMBIC_INI))
            config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
            with _preserved_logging():
                command.upgrade(config, "head")
            self.credentials = self._seed(url)
        except BaseException:
            self.close()
            raise
        return self

    def _seed(self, url: str) -> Credentials:
        from app.iam.credentials import issue_credential
        from scripts.issue_credential import _assign_profile, _resolve_principal
        from scripts.seed_demo import seed_demo

        engine = create_engine(url)
        maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        try:
            with maker() as session:
                seed_demo(session, organization_id=ORGANIZATION_ID, anchor=self.anchor)
                tokens = {}
                for name, principal_type, profile in (
                    ("eval-sandbox-inbound", "integration", "n8n-inbound"),
                    ("eval-airy-recepcion", "agent", "sales-agent-v0"),
                ):
                    principal = _resolve_principal(
                        session,
                        organization_id=ORGANIZATION_ID,
                        name=name,
                        principal_type=principal_type,
                    )
                    _assign_profile(
                        session,
                        organization_id=ORGANIZATION_ID,
                        principal_id=principal.id,
                        profile=profile,
                    )
                    _credential, tokens[profile] = issue_credential(
                        session,
                        organization_id=ORGANIZATION_ID,
                        principal_id=principal.id,
                        name=name,
                    )
                session.commit()
        finally:
            # A template must have no open connection to be cloned.
            engine.dispose()
        return Credentials(
            inbound_token=tokens["n8n-inbound"], agent_token=tokens["sales-agent-v0"]
        )

    @contextmanager
    def trial(self) -> Iterator[tuple[str, str]]:
        """Yield ``(canonical_url, agent_memory_url)`` for one isolated trial."""
        self._sequence += 1
        canonical = f"{self.template_name}_t{self._sequence}"
        memory = f"{self.template_name}_m{self._sequence}"
        try:
            canonical_url = self._create(canonical, template=self.template_name)
            memory_url = self._create(memory)
            yield canonical_url, memory_url
        finally:
            self._drop(memory)
            self._drop(canonical)

    def close(self) -> None:
        for name in list(reversed(self._created)):
            self._drop(name)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def new_world(database: EvalDatabase, scenario: Any) -> World:
    return World(
        anchor=database.anchor,
        external_contact_id=f"eval-{scenario.key}-{uuid4().hex[:8]}",
        phone_e164=DEFAULT_PHONE,
    )


def run_conversation(
    database: EvalDatabase,
    scenario: Any,
    world: World,
    *,
    maker: sessionmaker,
    memory_url: str,
    model: Any | None,
    agent_settings: Any,
) -> None:
    """Prepare the scenario, snapshot, and play its messages into ``world``.

    Failures propagate; the runner records them as a failed trial and still
    evaluates the checks against whatever state was reached.
    """
    from fastapi.testclient import TestClient

    from app import create_app as create_backend_app
    from app.db import get_db
    from integrations.sandbox.sender import SandboxInboundSender
    from sales_agent.api import create_app as create_sales_agent_app
    from sales_agent.gateway import BackendGateway
    from sales_agent.memory import PostgresAgentMemory
    from sales_agent.runtime import SalesAgentRuntime

    credentials = database.credentials
    assert credentials is not None, "EvalDatabase must be entered before a trial"
    with maker() as session:
        if scenario.given is not None:
            scenario.given(session, world)
            session.commit()
        world.before = snapshot(session)

    backend_app = create_backend_app()

    def _db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    backend_app.dependency_overrides[get_db] = _db
    backend_app.state.auth_sessionmaker = maker
    settings = replace(
        agent_settings,
        backend_base_url=BACKEND_BASE_URL,
        backend_credential=credentials.agent_token,
        agent_database_url=memory_url,
    )
    with (
        PostgresAgentMemory.open(memory_url, setup=True) as memory,
        TestClient(backend_app, base_url=BACKEND_BASE_URL) as backend_client,
    ):
        runtime = SalesAgentRuntime(
            gateway=BackendGateway(
                settings.backend_base_url, credentials.agent_token, http_client=backend_client
            ),
            model=model,
            checkpointer=memory.checkpointer,
            settings=settings,
        )
        sales_app = create_sales_agent_app(runtime=runtime, settings=settings)
        sales_app.state.auth_sessionmaker = maker
        with TestClient(sales_app) as sales_client:
            sender = SandboxInboundSender(
                backend_client=backend_client,
                sales_agent_client=sales_client,
                inbound_token=credentials.inbound_token,
                agent_token=credentials.agent_token,
            )
            for index, message in enumerate(scenario.render(world)):
                result = sender.send(
                    {
                        "schema_version": "1.0",
                        "provider": "sandbox",
                        "channel_account_external_id": SANDBOX_CHANNEL,
                        "provider_message_id": f"{world.external_contact_id}-{index}",
                        "external_contact_id": world.external_contact_id,
                        "phone_e164": world.phone_e164,
                        "message_type": "text",
                        "text": message,
                        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    }
                )
                world.conversation_id = result.conversation_id
                world.replies.append(result.agent_response or {})


__all__ = ["Credentials", "DATABASE_PREFIX", "EvalDatabase", "new_world", "run_conversation"]

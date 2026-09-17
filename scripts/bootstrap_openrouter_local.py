#!/usr/bin/env python3
"""Prepare the loopback Sales Agent/OpenRouter development environment.

This command performs no model or provider request. It uses the existing
PostgreSQL IAM credential issuer, creates only local agent-memory/catalog data,
and appends a managed override block to the ignored ``.env.local`` file.
Existing values outside that block, including secrets, are preserved.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.tenancy import BOOTSTRAP_ORGANIZATION_ID  # noqa: E402
from scripts.preflight_sales_agent import DEFAULT_ENV_FILE  # noqa: E402

LOCAL_CORE_DATABASE_URL = (
    "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow"
)
LOCAL_TEST_DATABASE_URL = (
    "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_test"
)
LOCAL_AGENT_DATABASE_URL = (
    "postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_agent"
)
LOCAL_BACKEND_URL = "http://127.0.0.1:8000"
LOCAL_RECEIVER_URL = f"{LOCAL_BACKEND_URL}/internal/sandbox/receive"
SANDBOX_CHANNEL_ACCOUNT_EXTERNAL_ID = "sandbox-local"

BLOCK_START = "# --- OPENROUTER-RUNTIME-01 local bootstrap (managed; secrets preserved) ---"
BLOCK_END = "# --- end OPENROUTER-RUNTIME-01 local bootstrap ---"

CREDENTIAL_SPECS = (
    ("SALES_AGENT_V0_CREDENTIAL", "local-sales-agent-v0", "agent", "sales-agent-v0"),
    ("SANDBOX_INBOUND_TOKEN", "local-sandbox-inbound", "integration", "n8n-inbound"),
    (
        "SANDBOX_DISPATCHER_TOKEN",
        "local-sandbox-dispatcher",
        "integration",
        "outbound-dispatcher",
    ),
)


class BootstrapError(RuntimeError):
    """The safe local bootstrap contract could not be completed."""


def _parse_env_assignments(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        if not separator or not key.isidentifier():
            continue
        try:
            parsed = shlex.split(raw_value, comments=False)
        except ValueError:
            continue
        values[key] = parsed[0] if parsed else ""
    return values


def _shell_assignment(key: str, value: str) -> str:
    return f"{key}={shlex.quote(value)}"


def update_env_file(path: Path, values: Mapping[str, str]) -> None:
    """Append the managed block once; never rewrite existing lines or values."""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if BLOCK_START in original and BLOCK_END in original:
        block_start = original.index(BLOCK_START)
        block_end = original.index(BLOCK_END, block_start)
        block = original[block_start:block_end]
        present = set(_parse_env_assignments(block))
        missing = [key for key in values if key not in present]
        if not missing:
            path.chmod(0o600)
            return
        insertion = "\n".join(_shell_assignment(key, values[key]) for key in missing)
        updated = original[:block_end] + insertion + "\n" + original[block_end:]
    else:
        prefix = original if not original or original.endswith("\n") else original + "\n"
        block = "\n".join(
            [BLOCK_START]
            + [_shell_assignment(key, value) for key, value in values.items()]
            + [BLOCK_END, ""]
        )
        updated = prefix + ("\n" if prefix else "") + block

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(updated)
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, mode)
    os.replace(temporary_path, path)


def _run_local_migrations() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": LOCAL_CORE_DATABASE_URL,
            "TEST_DATABASE_URL": LOCAL_TEST_DATABASE_URL,
            "SALES_AGENT_DATABASE_URL": LOCAL_AGENT_DATABASE_URL,
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        raise BootstrapError("The loopback PostgreSQL migration did not complete.")


def _ensure_agent_database() -> None:
    server_url = make_url(LOCAL_CORE_DATABASE_URL).set(database="postgres")
    engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = 'odontoflow_agent'")
            )
            if not exists:
                connection.exec_driver_sql('CREATE DATABASE "odontoflow_agent"')
    except Exception as exc:
        raise BootstrapError(
            "The separate loopback Sales Agent memory database could not be prepared."
        ) from exc
    finally:
        engine.dispose()

    try:
        from sales_agent.memory import setup_agent_memory

        setup_agent_memory(LOCAL_AGENT_DATABASE_URL)
    except Exception as exc:
        raise BootstrapError("The Sales Agent memory schema could not be prepared.") from exc


def _ensure_sandbox_channel(session) -> None:
    from app.messaging.models import ChannelAccount

    channel = session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.organization_id == BOOTSTRAP_ORGANIZATION_ID,
            ChannelAccount.provider == "sandbox",
            ChannelAccount.external_account_id == SANDBOX_CHANNEL_ACCOUNT_EXTERNAL_ID,
        )
    )
    if channel is None:
        session.add(
            ChannelAccount(
                organization_id=BOOTSTRAP_ORGANIZATION_ID,
                provider="sandbox",
                external_account_id=SANDBOX_CHANNEL_ACCOUNT_EXTERNAL_ID,
                phone_number_id=None,
                display_name="OdontoFlow local sandbox",
                is_active=True,
            )
        )
    else:
        channel.display_name = "OdontoFlow local sandbox"
        channel.is_active = True


def _credential_is_valid(
    session,
    token: str,
    *,
    principal_type: str,
    profile: str,
) -> bool:
    from app.iam.credentials import authenticate
    from app.iam.models import Principal
    from app.iam.service import effective_permission_codes
    from scripts.issue_credential import PROFILE_PERMISSIONS

    try:
        credential = authenticate(session, token)
    except Exception:
        return False
    if credential.organization_id != BOOTSTRAP_ORGANIZATION_ID:
        return False
    principal = session.get(Principal, credential.principal_id)
    if principal is None or principal.type != principal_type:
        return False
    permissions = effective_permission_codes(
        session,
        principal.id,
        BOOTSTRAP_ORGANIZATION_ID,
    )
    return set(PROFILE_PERMISSIONS[profile]).issubset(permissions)


def _issue_or_preserve_credentials(
    session,
    existing_values: Mapping[str, str],
) -> dict[str, str]:
    from app.iam.credentials import issue_credential
    from scripts.issue_credential import _assign_profile, _resolve_principal

    credentials: dict[str, str] = {}
    for env_name, principal_name, principal_type, profile in CREDENTIAL_SPECS:
        existing = os.environ.get(env_name, "").strip() or existing_values.get(env_name, "").strip()
        if existing:
            if not _credential_is_valid(
                session,
                existing,
                principal_type=principal_type,
                profile=profile,
            ):
                raise BootstrapError(
                    f"{env_name} is set but is not a valid local {profile} credential; "
                    "refusing to overwrite it."
                )
            credentials[env_name] = existing
            continue

        principal = _resolve_principal(
            session,
            organization_id=BOOTSTRAP_ORGANIZATION_ID,
            name=principal_name,
            principal_type=principal_type,
        )
        _assign_profile(
            session,
            organization_id=BOOTSTRAP_ORGANIZATION_ID,
            principal_id=principal.id,
            profile=profile,
        )
        _credential, token = issue_credential(
            session,
            organization_id=BOOTSTRAP_ORGANIZATION_ID,
            principal_id=principal.id,
            name=principal_name,
        )
        credentials[env_name] = token
    return credentials


def bootstrap(env_file: Path = DEFAULT_ENV_FILE) -> None:
    existing_content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    existing_values = _parse_env_assignments(existing_content)

    _run_local_migrations()
    _ensure_agent_database()

    os.environ.update(
        {
            "DATABASE_URL": LOCAL_CORE_DATABASE_URL,
            "TEST_DATABASE_URL": LOCAL_TEST_DATABASE_URL,
            "SALES_AGENT_DATABASE_URL": LOCAL_AGENT_DATABASE_URL,
        }
    )
    from app.db import SessionLocal
    from scripts.seed_reception_demo import seed_reception_demo

    with SessionLocal() as session:
        seed_reception_demo(session, organization_id=BOOTSTRAP_ORGANIZATION_ID)
        _ensure_sandbox_channel(session)
        credentials = _issue_or_preserve_credentials(session, existing_values)
        session.commit()

    values = {
        "APP_ENV": "development",
        "DATABASE_URL": LOCAL_CORE_DATABASE_URL,
        "TEST_DATABASE_URL": LOCAL_TEST_DATABASE_URL,
        "ERP_ANONYMOUS_COMPAT": "true",
        "INTEGRATION_API_ENABLED": "true",
        "API_HOST": "127.0.0.1",
        "API_PORT": "8000",
        "SALES_AGENT_DATABASE_URL": LOCAL_AGENT_DATABASE_URL,
        "SALES_AGENT_BACKEND_URL": LOCAL_BACKEND_URL,
        "SALES_AGENT_MODEL_PROVIDER": "openrouter",
        "SALES_AGENT_MODEL": "deepseek/deepseek-v4-flash-0731",
        "SALES_AGENT_MODEL_BASE_URL": "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", "").strip()
        or existing_values.get("OPENROUTER_API_KEY", "").strip(),
        "SALES_AGENT_V0_CREDENTIAL": credentials["SALES_AGENT_V0_CREDENTIAL"],
        "SALES_AGENT_RECURSION_LIMIT": "12",
        "SALES_AGENT_REQUEST_TIMEOUT_SECONDS": "10.0",
        "SALES_AGENT_MODEL_TIMEOUT_SECONDS": "20.0",
        "SALES_AGENT_TURN_TIMEOUT_SECONDS": "180.0",
        "SALES_AGENT_MODEL_MAX_OUTPUT_TOKENS": "512",
        "SANDBOX_BACKEND_URL": LOCAL_BACKEND_URL,
        "SANDBOX_RECEIVER_URL": LOCAL_RECEIVER_URL,
        "SANDBOX_CHANNEL_ACCOUNT_EXTERNAL_ID": SANDBOX_CHANNEL_ACCOUNT_EXTERNAL_ID,
        "SANDBOX_INBOUND_TOKEN": credentials["SANDBOX_INBOUND_TOKEN"],
        "SANDBOX_DISPATCHER_TOKEN": credentials["SANDBOX_DISPATCHER_TOKEN"],
        "SANDBOX_REQUEST_TIMEOUT_SECONDS": "10.0",
    }
    update_env_file(env_file, values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = parser.parse_args()
    bootstrap(args.env_file)
    print("Local OpenRouter environment prepared; no model request was made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

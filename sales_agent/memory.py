"""Durable Sales Agent working memory backed by a separate PostgreSQL database."""

from __future__ import annotations

from typing import Any

from sales_agent.config import get_settings, validate_agent_database_url


class PostgresAgentMemory:
    """Keep one synchronous ``PostgresSaver`` open for the process lifetime.

    ``setup`` is intentionally explicit. Deployment/tests can create the
    checkpointer tables once, while ordinary process construction only opens
    an already-provisioned ``odontoflow_agent`` database.
    """

    def __init__(self, database_url: str) -> None:
        validated = validate_agent_database_url(database_url)
        # ``PostgresSaver`` passes this value directly to psycopg, whose
        # conninfo parser accepts ``postgresql://`` but not SQLAlchemy's
        # ``postgresql+psycopg://`` driver decoration.
        saver_url = validated.set(drivername="postgresql")
        self.database_url = saver_url.render_as_string(hide_password=False)
        self._context_manager: Any | None = None
        self.checkpointer: Any | None = None

    @classmethod
    def open(
        cls,
        database_url: str | None = None,
        *,
        setup: bool = False,
    ) -> "PostgresAgentMemory":
        memory = cls(database_url or get_settings().agent_database_url)
        if setup:
            memory.setup()
        from langgraph.checkpoint.postgres import PostgresSaver

        memory._context_manager = PostgresSaver.from_conn_string(memory.database_url)
        memory.checkpointer = memory._context_manager.__enter__()
        return memory

    def setup(self) -> None:
        """Create the LangGraph tables in the separate database if absent."""
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(self.database_url) as checkpointer:
            checkpointer.setup()

    def close(self) -> None:
        if self._context_manager is not None:
            self._context_manager.__exit__(None, None, None)
            self._context_manager = None
            self.checkpointer = None

    def __enter__(self) -> "PostgresAgentMemory":
        if self.checkpointer is None:
            raise RuntimeError("PostgresAgentMemory.open() must be used before entering.")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def setup_agent_memory(database_url: str | None = None) -> None:
    """Explicitly provision the separate agent-memory database tables."""
    memory = PostgresAgentMemory(database_url or get_settings().agent_database_url)
    memory.setup()


__all__ = ["PostgresAgentMemory", "setup_agent_memory"]

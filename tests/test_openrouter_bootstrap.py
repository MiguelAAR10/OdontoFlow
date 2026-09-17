"""Idempotent local env-file update proofs for OPENROUTER-RUNTIME-01."""

from __future__ import annotations

from pathlib import Path

from scripts.bootstrap_openrouter_local import update_env_file


def test_update_env_file_preserves_existing_values_and_unrelated_settings(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "OWNER_SECRET=keep-this-value\n"
        "UNRELATED_SETTING=preserve-this-setting\n"
        "DATABASE_URL=owner-value-is-preserved\n",
        encoding="utf-8",
    )

    update_env_file(
        env_file,
        {
            "DATABASE_URL": "local-database-value",
            "OPENROUTER_API_KEY": "",
            "SALES_AGENT_V0_CREDENTIAL": "generated-agent-token",
        },
    )

    first = env_file.read_text(encoding="utf-8")
    assert "OWNER_SECRET=keep-this-value" in first
    assert "UNRELATED_SETTING=preserve-this-setting" in first
    assert "DATABASE_URL=owner-value-is-preserved" in first
    assert "DATABASE_URL=local-database-value" in first
    assert "SALES_AGENT_V0_CREDENTIAL=generated-agent-token" in first

    update_env_file(
        env_file,
        {
            "DATABASE_URL": "a-different-database-value",
            "OPENROUTER_API_KEY": "replacement-must-not-overwrite",
            "SALES_AGENT_V0_CREDENTIAL": "replacement-must-not-overwrite",
        },
    )

    second = env_file.read_text(encoding="utf-8")
    assert second == first

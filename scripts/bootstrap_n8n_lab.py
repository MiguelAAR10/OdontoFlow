"""Provision the local ODONTO SMART n8n lab without printing its secrets."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.iam.credentials import (  # noqa: E402
    IntegrationCredential,
    issue_credential,
    revoke_credential,
)
from app.messaging.models import ChannelAccount  # noqa: E402
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID  # noqa: E402
from scripts.issue_credential import _assign_profile, _resolve_principal  # noqa: E402
from scripts.seed_reception_demo import seed_reception_demo  # noqa: E402


LAB_ACCOUNT = "odonto-smart-lab"
LAB_CREDENTIALS = (
    ("ODONTOFLOW_INBOUND_TOKEN", "n8n-lab-inbound", "integration", "n8n-inbound"),
    ("ODONTOFLOW_AGENT_TOKEN", "n8n-lab-agent", "agent", "conversation-agent"),
    (
        "ODONTOFLOW_OPERATOR_TOKEN",
        "n8n-lab-operator",
        "integration",
        "reception-operator",
    ),
)


def provision_n8n_lab(
    session: Session,
    *,
    organization_id: int = BOOTSTRAP_ORGANIZATION_ID,
    base_url: str = "http://127.0.0.1:8000",
) -> dict[str, str]:
    """Seed the synthetic clinic and rotate three least-privilege lab tokens."""
    seed_reception_demo(session, organization_id=organization_id)
    channel = session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.organization_id == organization_id,
            ChannelAccount.provider == "test",
            ChannelAccount.external_account_id == LAB_ACCOUNT,
        )
    )
    if channel is None:
        channel = ChannelAccount(
            organization_id=organization_id,
            provider="test",
            external_account_id=LAB_ACCOUNT,
            phone_number_id=None,
            display_name="Simulador n8n ODONTO SMART",
            is_active=True,
        )
        session.add(channel)
        session.flush()
    else:
        channel.display_name = "Simulador n8n ODONTO SMART"
        channel.is_active = True

    config = {
        "ODONTOFLOW_BASE_URL": base_url.rstrip("/"),
        "ODONTOFLOW_PROVIDER": "test",
        "ODONTOFLOW_CHANNEL_ACCOUNT_ID": str(channel.id),
    }
    for env_name, principal_name, principal_type, profile in LAB_CREDENTIALS:
        principal = _resolve_principal(
            session,
            organization_id=organization_id,
            name=principal_name,
            principal_type=principal_type,
        )
        _assign_profile(
            session,
            organization_id=organization_id,
            principal_id=principal.id,
            profile=profile,
        )
        previous = session.scalars(
            select(IntegrationCredential).where(
                IntegrationCredential.organization_id == organization_id,
                IntegrationCredential.name == principal_name,
                IntegrationCredential.revoked_at.is_(None),
            )
        ).all()
        for credential in previous:
            revoke_credential(session, credential.id)
        _credential, token = issue_credential(
            session,
            organization_id=organization_id,
            principal_id=principal.id,
            name=principal_name,
        )
        config[env_name] = token
    session.flush()
    return config


def write_lab_env(path: Path, config: dict[str, str]) -> None:
    """Write the one-time tokens without echoing them to stdout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered_keys = (
        "ODONTOFLOW_BASE_URL",
        "ODONTOFLOW_PROVIDER",
        "ODONTOFLOW_CHANNEL_ACCOUNT_ID",
        "ODONTOFLOW_INBOUND_TOKEN",
        "ODONTOFLOW_AGENT_TOKEN",
        "ODONTOFLOW_OPERATOR_TOKEN",
    )
    path.write_text(
        "".join(f"{key}={config[key]}\n" for key in ordered_keys),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=int, default=BOOTSTRAP_ORGANIZATION_ID)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path(".env.n8n.local"))
    args = parser.parse_args()
    target = args.output.resolve()
    temporary = target.with_name(f"{target.name}.tmp")

    with SessionLocal() as session:
        config = provision_n8n_lab(
            session,
            organization_id=args.organization,
            base_url=args.base_url,
        )
        write_lab_env(temporary, config)
        try:
            session.commit()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, target)
    print(
        "Laboratorio n8n provisionado. "
        f"Canal test id={config['ODONTOFLOW_CHANNEL_ACCOUNT_ID']}; "
        f"secretos guardados en {target}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

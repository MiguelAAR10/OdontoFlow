from pathlib import Path

from sqlalchemy import select

from app.iam.credentials import IntegrationCredential, split_token
from app.messaging.models import ChannelAccount
from scripts.bootstrap_n8n_lab import provision_n8n_lab, write_lab_env


ORG = 1


def test_n8n_lab_bootstrap_provisions_test_channel_and_separate_secrets(
    session, tmp_path: Path
):
    config = provision_n8n_lab(
        session,
        organization_id=ORG,
        base_url="http://127.0.0.1:8000",
    )
    session.commit()
    target = tmp_path / ".env.n8n.local"
    write_lab_env(target, config)

    channel = session.scalar(
        select(ChannelAccount).where(
            ChannelAccount.organization_id == ORG,
            ChannelAccount.provider == "test",
            ChannelAccount.external_account_id == "odonto-smart-lab",
        )
    )
    credentials = session.scalars(
        select(IntegrationCredential).where(
            IntegrationCredential.organization_id == ORG,
            IntegrationCredential.name.in_(
                (
                    "n8n-lab-inbound",
                    "n8n-lab-agent",
                    "n8n-lab-operator",
                )
            ),
            IntegrationCredential.revoked_at.is_(None),
        )
    ).all()

    assert channel is not None
    assert config["ODONTOFLOW_CHANNEL_ACCOUNT_ID"] == str(channel.id)
    assert len(credentials) == 3
    assert len({row.principal_id for row in credentials}) == 3
    assert all(
        split_token(config[key]) is not None
        for key in (
            "ODONTOFLOW_INBOUND_TOKEN",
            "ODONTOFLOW_AGENT_TOKEN",
            "ODONTOFLOW_OPERATOR_TOKEN",
        )
    )
    contents = target.read_text(encoding="utf-8")
    assert "ODONTOFLOW_PROVIDER=test" in contents
    assert "ODONTOFLOW_BASE_URL=http://127.0.0.1:8000" in contents
    assert contents.endswith("\n")

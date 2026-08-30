"""Create or update a tenant-owned WhatsApp channel account without secrets."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.messaging.models import ChannelAccount  # noqa: E402
from app.tenancy import BOOTSTRAP_ORGANIZATION_ID  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=int, default=BOOTSTRAP_ORGANIZATION_ID)
    parser.add_argument("--external-account-id", required=True)
    parser.add_argument("--phone-number-id")
    parser.add_argument("--display-name", required=True)
    args = parser.parse_args()

    with SessionLocal.begin() as session:
        account = session.scalar(
            select(ChannelAccount).where(
                ChannelAccount.organization_id == args.organization,
                ChannelAccount.provider == "whatsapp",
                ChannelAccount.external_account_id == args.external_account_id,
            )
        )
        if account is None:
            account = ChannelAccount(
                organization_id=args.organization,
                provider="whatsapp",
                external_account_id=args.external_account_id,
                phone_number_id=args.phone_number_id,
                display_name=args.display_name,
            )
            session.add(account)
            session.flush()
            action = "creada"
        else:
            account.phone_number_id = args.phone_number_id
            account.display_name = args.display_name
            account.is_active = True
            session.flush()
            action = "actualizada"

        print(
            f"Cuenta WhatsApp {action}: id={account.id} "
            f"organization_id={account.organization_id}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


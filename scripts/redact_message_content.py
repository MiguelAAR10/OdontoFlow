"""Redact expired message content in bounded PostgreSQL batches."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.messaging.service import redact_expired_message_content  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--organization-id",
        type=int,
        required=True,
        help="Tenant whose expired message content may be redacted.",
    )
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    if args.organization_id < 1:
        parser.error("--organization-id must be a positive integer")
    if not 1 <= args.limit <= 5000:
        parser.error("--limit must be between 1 and 5000")

    with SessionLocal() as session:
        count = redact_expired_message_content(
            session,
            organization_id=args.organization_id,
            limit=args.limit,
        )
    print(f"Redacted message contents: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

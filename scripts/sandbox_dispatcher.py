"""Deliver due development-only sandbox messages once and exit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.sandbox.consumer import (  # noqa: E402
    SandboxConfigurationError,
    SandboxConsumer,
    SandboxConsumerError,
    SandboxSettings,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    try:
        settings = SandboxSettings.from_env()
        with SandboxConsumer(settings) as consumer:
            result = consumer.dispatch_once(limit=args.limit)
    except SandboxConfigurationError as exc:
        print(f"Sandbox dispatcher configuration error: {exc}", file=sys.stderr)
        return 2
    except SandboxConsumerError as exc:
        print(f"Sandbox dispatcher failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"claimed={result.claimed} delivered={result.delivered} failed={result.failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

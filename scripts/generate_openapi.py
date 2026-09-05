"""Regenerate the committed OpenAPI JSON and YAML from the FastAPI factory."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402


def main() -> None:
    schema = create_app().openapi()
    target = ROOT / "docs" / "api"
    target.mkdir(parents=True, exist_ok=True)
    (target / "openapi.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (target / "openapi.yaml").write_text(
        yaml.safe_dump(schema, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()


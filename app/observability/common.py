"""Shared pieces of the B3 staff reads: the human-only gate and keyset cursors.

Cursors follow the B2 inbox pattern (``app/proposals/service.py``): an opaque
url-safe base64 JSON array of the sort key. A cursor that does not decode to the
expected shape is ``INVALID_INPUT`` (422), never a 500.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime

from app.errors import AppError, ErrorCode
from app.iam.context import ExecutionContext
from app.iam.service import (
    PERMISSION_DENIED_HTTP_STATUS,
    PERMISSION_DENIED_MESSAGE,
    IamErrorCode,
)


def require_human(ctx: ExecutionContext) -> None:
    """Staff surfaces are for people: agents keep their ``/internal`` and tool reads."""
    if ctx.principal_type != "human":
        raise AppError(
            IamErrorCode.PERMISSION_DENIED,
            PERMISSION_DENIED_MESSAGE,
            details={},
            http_status=PERMISSION_DENIED_HTTP_STATUS,
        )


def encode_cursor(*values: datetime | str | int) -> str:
    raw = [v.isoformat() if isinstance(v, datetime) else v for v in values]
    payload = json.dumps(raw, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str, shape: tuple[type, ...]) -> tuple:
    """Decode to ``shape`` (``datetime`` must be tz-aware, ``str``, ``int``)."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(raw, list) or len(raw) != len(shape):
            raise ValueError("shape")
        values = []
        for kind, value in zip(shape, raw):
            if kind is datetime:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError("naive")
                values.append(parsed)
            elif kind is int:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError("int")
                values.append(value)
            else:
                if not isinstance(value, str):
                    raise ValueError("str")
                values.append(value)
        return tuple(values)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AppError(ErrorCode.INVALID_INPUT, "The cursor is invalid.") from exc

from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

HTTP_STATUS_BY_CODE = {
    "INVALID_INPUT": 422,
    "NOT_FOUND": 404,
    "ENTITY_INACTIVE": 409,
    "CAPABILITY_MISSING": 409,
    "SLOT_BLOCKED": 409,
    "APPOINTMENT_CONFLICT": 409,
    "IDEMPOTENCY_KEY_REUSED": 409,
}

DEFAULT_MESSAGE_BY_CODE = {
    "INVALID_INPUT": "The request is invalid.",
    "NOT_FOUND": "The requested resource was not found.",
    "ENTITY_INACTIVE": "The referenced entity is inactive.",
    "CAPABILITY_MISSING": "The practitioner is not capable of this service at this location.",
    "SLOT_BLOCKED": "The requested time slot is blocked.",
    "APPOINTMENT_CONFLICT": "The requested appointment slot is no longer available.",
    "IDEMPOTENCY_KEY_REUSED": "The idempotency key was already used by a different request.",
}

POSTGRES_EXCLUSION_VIOLATION = "23P01"


class ErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    ENTITY_INACTIVE = "ENTITY_INACTIVE"
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    SLOT_BLOCKED = "SLOT_BLOCKED"
    APPOINTMENT_CONFLICT = "APPOINTMENT_CONFLICT"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        details: dict | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGE_BY_CODE[code.value]
        self.details = details or {}
        self.http_status = http_status or HTTP_STATUS_BY_CODE[code.value]
        super().__init__(self.message)


def _error_payload(code: ErrorCode, message: str, details: dict) -> dict:
    return {"error": {"code": code.value, "message": message, "details": details}}


def _sqlstate(exc: IntegrityError) -> str | None:
    orig = exc.orig
    if orig is None:
        return None
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if state is None:
        diag = getattr(orig, "diag", None)
        state = getattr(diag, "sqlstate", None) if diag is not None else None
    return str(state) if state else None


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_STATUS_BY_CODE[ErrorCode.INVALID_INPUT.value],
            content=_error_payload(
                ErrorCode.INVALID_INPUT,
                DEFAULT_MESSAGE_BY_CODE[ErrorCode.INVALID_INPUT.value],
                {},
            ),
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(
        request: Request, exc: IntegrityError
    ) -> JSONResponse:
        if _sqlstate(exc) == POSTGRES_EXCLUSION_VIOLATION:
            return JSONResponse(
                status_code=HTTP_STATUS_BY_CODE[ErrorCode.APPOINTMENT_CONFLICT.value],
                content=_error_payload(
                    ErrorCode.APPOINTMENT_CONFLICT,
                    DEFAULT_MESSAGE_BY_CODE[ErrorCode.APPOINTMENT_CONFLICT.value],
                    {},
                ),
            )
        raise exc

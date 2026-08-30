"""ASGI transport controls applied before request parsing and domain code."""

from __future__ import annotations

from uuid import UUID, uuid4

from starlette.responses import JSONResponse

from app.config import Settings

REQUEST_ID_HEADER = "X-Request-Id"
CORRELATION_ID_HEADER = "X-Correlation-Id"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
HSTS_VALUE = "max-age=31536000; includeSubDomains"
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
HTTP_OPERATION_METHODS = {"get", "post", "put", "patch", "delete"}


def new_uuid() -> str:
    return str(uuid4())


def _valid_uuid(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return None


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": {}}},
    )


class SecurityBoundaryMiddleware:
    """Validate traces, bound bodies, enforce TLS and decorate every response."""

    def __init__(self, app, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        raw_request_id = raw_headers.get(REQUEST_ID_HEADER.lower())
        raw_correlation_id = raw_headers.get(CORRELATION_ID_HEADER.lower())
        request_id = _valid_uuid(raw_request_id) if raw_request_id else new_uuid()
        correlation_id = (
            _valid_uuid(raw_correlation_id) if raw_correlation_id else request_id
        )
        response_request_id = request_id or new_uuid()
        response_correlation_id = correlation_id or response_request_id

        state = scope.setdefault("state", {})
        state["request_id"] = response_request_id
        state["correlation_id"] = response_correlation_id

        async def secured_send(message) -> None:
            if message["type"] == "http.response.start":
                protected = {
                    key.lower().encode("latin-1")
                    for key in (
                        *SECURITY_HEADERS,
                        REQUEST_ID_HEADER,
                        CORRELATION_ID_HEADER,
                        "Strict-Transport-Security",
                    )
                }
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in protected
                ]
                headers.extend(
                    (key.encode("latin-1"), value.encode("latin-1"))
                    for key, value in SECURITY_HEADERS.items()
                )
                headers.extend(
                    (
                        (REQUEST_ID_HEADER.encode("latin-1"), response_request_id.encode()),
                        (
                            CORRELATION_ID_HEADER.encode("latin-1"),
                            response_correlation_id.encode(),
                        ),
                    )
                )
                if scope.get("scheme") == "https":
                    headers.append(
                        (b"Strict-Transport-Security", HSTS_VALUE.encode("latin-1"))
                    )
                message["headers"] = headers
            await send(message)

        if raw_request_id and request_id is None:
            await _error(
                "INVALID_INPUT", "X-Request-Id must be a UUID.", 422
            )(scope, receive, secured_send)
            return
        if raw_correlation_id and correlation_id is None:
            await _error(
                "INVALID_INPUT", "X-Correlation-Id must be a UUID.", 422
            )(scope, receive, secured_send)
            return
        if self.settings.require_https and scope.get("scheme") != "https":
            await _error(
                "HTTPS_REQUIRED", "HTTPS is required for this API.", 400
            )(scope, receive, secured_send)
            return

        content_length = raw_headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = self.settings.max_json_body_bytes + 1
            if declared_length > self.settings.max_json_body_bytes:
                await _error(
                    "PAYLOAD_TOO_LARGE",
                    "The request body exceeds the configured limit.",
                    413,
                )(scope, receive, secured_send)
                return

        if scope.get("method") in BODY_METHODS:
            buffered: list[dict] = []
            total = 0
            while True:
                message = await receive()
                buffered.append(message)
                if message["type"] == "http.disconnect":
                    return
                if message["type"] == "http.request":
                    total += len(message.get("body", b""))
                    if total > self.settings.max_json_body_bytes:
                        await _error(
                            "PAYLOAD_TOO_LARGE",
                            "The request body exceeds the configured limit.",
                            413,
                        )(scope, receive, secured_send)
                        return
                    if not message.get("more_body", False):
                        break

            index = 0

            async def replay_receive():
                nonlocal index
                if index < len(buffered):
                    message = buffered[index]
                    index += 1
                    return message
                return {"type": "http.request", "body": b"", "more_body": False}

            receive = replay_receive

        await self.app(scope, receive, secured_send)


def install_security_openapi(app) -> None:
    """Publish the security/trace contract that middleware enforces at runtime."""
    original_openapi = app.openapi

    def security_openapi():
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original_openapi()
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})
        schemas["ErrorEnvelope"] = {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message", "details"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "details": {"type": "object"},
                    },
                }
            },
        }
        trace_header = {
            "description": "Canonical UUID used for end-to-end traceability.",
            "schema": {"type": "string", "format": "uuid"},
        }
        error_content = {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
            }
        }
        common_errors = {
            "401": "A valid integration credential is required.",
            "403": "The authenticated principal lacks the required permission.",
            "429": "The credential rate limit was exceeded.",
            "503": "The integration surface is disabled.",
        }

        for path, path_item in schema.get("paths", {}).items():
            for method, operation in path_item.items():
                if method.lower() not in HTTP_OPERATION_METHODS:
                    continue
                responses = operation.setdefault("responses", {})
                for response in responses.values():
                    headers = response.setdefault("headers", {})
                    headers.setdefault("X-Request-Id", trace_header.copy())
                    headers.setdefault("X-Correlation-Id", trace_header.copy())
                if path == "/health":
                    continue

                parameters = operation.setdefault("parameters", [])
                existing = {
                    (parameter.get("in"), parameter.get("name"))
                    for parameter in parameters
                }
                for header_name in (REQUEST_ID_HEADER, CORRELATION_ID_HEADER):
                    if ("header", header_name) not in existing:
                        parameters.append(
                            {
                                "in": "header",
                                "name": header_name,
                                "required": False,
                                "schema": {"type": "string", "format": "uuid"},
                            }
                        )
                for status, description in common_errors.items():
                    response = responses.setdefault(
                        status,
                        {"description": description, "content": error_content},
                    )
                    headers = response.setdefault("headers", {})
                    headers.setdefault("X-Request-Id", trace_header.copy())
                    headers.setdefault("X-Correlation-Id", trace_header.copy())
                responses["429"].setdefault("headers", {})["Retry-After"] = {
                    "description": "Seconds until the current rate-limit window resets.",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 60},
                }
                if method.upper() in BODY_METHODS:
                    response = responses.setdefault(
                        "413",
                        {
                            "description": "The request body exceeds the configured limit.",
                            "content": error_content,
                        },
                    )
                    headers = response.setdefault("headers", {})
                    headers.setdefault("X-Request-Id", trace_header.copy())
                    headers.setdefault("X-Correlation-Id", trace_header.copy())

        app.openapi_schema = schema
        return schema

    app.openapi = security_openapi


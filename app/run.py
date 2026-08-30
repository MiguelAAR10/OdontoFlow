"""Hardened Uvicorn entrypoint for the OdontoFlow API."""

from __future__ import annotations

import ipaddress
import os

import uvicorn


def _trusted_proxies() -> tuple[str, ...]:
    values = tuple(
        item.strip()
        for item in os.environ.get("TRUSTED_PROXY_IPS", "").split(",")
        if item.strip()
    )
    for value in values:
        ipaddress.ip_address(value)
    return values


def main() -> None:
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    host = os.environ.get("API_HOST", "127.0.0.1").strip()
    port = int(os.environ.get("API_PORT", "8000"))
    certfile = os.environ.get("TLS_CERTFILE", "").strip() or None
    keyfile = os.environ.get("TLS_KEYFILE", "").strip() or None
    trusted_proxies = _trusted_proxies()

    if bool(certfile) != bool(keyfile):
        raise RuntimeError("TLS_CERTFILE and TLS_KEYFILE must be configured together.")
    if app_env == "production" and certfile is None and not trusted_proxies:
        raise RuntimeError(
            "Production requires local TLS or an explicit TRUSTED_PROXY_IPS allowlist."
        )

    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        server_header=False,
        proxy_headers=bool(trusted_proxies),
        forwarded_allow_ips=",".join(trusted_proxies) if trusted_proxies else None,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
    )


if __name__ == "__main__":
    main()


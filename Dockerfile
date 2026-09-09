# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.16 AS uv
FROM python:3.12-slim AS runtime

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /srv/app
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app app
COPY sales_agent sales_agent
COPY alembic alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 appuser
USER appuser

ENV API_HOST=0.0.0.0 \
    API_PORT=8000
EXPOSE 8000

CMD ["uv", "run", "--no-sync", "python", "-m", "app.run"]

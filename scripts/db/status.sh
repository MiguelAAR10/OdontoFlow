#!/usr/bin/env bash
# Read-only snapshot of DB target, migration state, and repo migration head.
# Never prints secret values (passwords are redacted from any URL shown).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [ -f .env.local ]; then
  set -a
  . ./.env.local
  set +a
fi

redact_url() {
  # postgresql+psycopg://user:PASSWORD@host:port/db -> postgresql+psycopg://user:***@host:port/db
  printf '%s' "$1" | sed -E 's#(://[^:/@]+):[^@/]+@#\1:***@#'
}

db_url="${DATABASE_URL:-postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow}"

echo "== configured DB target =="
echo "  $(redact_url "$db_url")"

echo "== alembic current (database) =="
uv run alembic -c alembic.ini current 2>&1 | sed 's/^/  /'

echo "== alembic heads (repository) =="
uv run alembic -c alembic.ini heads 2>&1 | sed 's/^/  /'

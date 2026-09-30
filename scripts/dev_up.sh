#!/usr/bin/env bash
# Bring the local demo backend up: DB container, migrations, demo seed,
# staff credential (first time only) and the API. Never prints secret values.
#
#   scripts/dev_up.sh             # ... and serve on API_PORT (default 8000)
#   scripts/dev_up.sh --no-serve  # stop after seeding
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SERVE=1
for arg in "$@"; do
  case "$arg" in
    --no-serve) SERVE=0 ;;
    *) echo "dev_up: argumento desconocido: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f .env.local ]; then
  echo "dev_up: falta .env.local" >&2
  exit 1
fi
set -a
. ./.env.local
set +a

if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
DB_CONTAINER="odontoflow-db-1"
ENV_DEMO_FILE=".env.demo.local"

# Host and port only — the URL itself (with its password) is never echoed.
read -r DB_HOST DB_PORT < <(
  "$PY" -c 'import os; from sqlalchemy.engine import make_url; u = make_url(os.environ["DATABASE_URL"]); print(u.host or "", u.port or 5432)'
)
case "$DB_HOST" in
  127.0.0.1|localhost|::1) ;;
  *) echo "dev_up: DATABASE_URL no apunta a 127.0.0.1/localhost/::1 (host: ${DB_HOST:-vacío}); abortado." >&2
     exit 1 ;;
esac

echo "== base de datos ($DB_HOST:$DB_PORT) =="
docker start "$DB_CONTAINER" >/dev/null
for _ in $(seq 1 30); do
  if pg_isready -q -h "$DB_HOST" -p "$DB_PORT"; then break; fi
  sleep 1
done
if ! pg_isready -q -h "$DB_HOST" -p "$DB_PORT"; then
  echo "dev_up: PostgreSQL no respondió en $DB_HOST:$DB_PORT" >&2
  exit 1
fi

echo "== migraciones =="
"$PY" -m alembic upgrade head

echo "== semilla demo =="
if [ -f "$ENV_DEMO_FILE" ]; then
  "$PY" scripts/seed_demo.py
else
  "$PY" scripts/seed_demo.py --issue-staff-credential --env-file "$ENV_DEMO_FILE"
fi

if [ "$SERVE" -eq 0 ]; then
  echo "== listo (--no-serve) =="
  exit 0
fi

echo "== API en ${API_HOST:-127.0.0.1}:${API_PORT:-8000} =="
exec "$PY" -m app.run

#!/usr/bin/env bash
# Verifies a fresh dev environment can run this backend. Never prints secret values.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

FAILED=0
pass() { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; }
fail() { printf '  [fail] %s\n' "$1"; FAILED=1; }

if [ -f .env.local ]; then
  set -a
  . ./.env.local
  set +a
fi

echo "== python =="
if command -v python3 >/dev/null 2>&1; then
  pyv=$(python3 --version 2>&1 | awk '{print $2}')
  expected=$(tr -d '[:space:]' < .python-version 2>/dev/null)
  if [ -n "$expected" ] && [[ "$pyv" != "$expected"* ]]; then
    warn "python3 is $pyv, .python-version wants $expected"
  else
    pass "python3 $pyv"
  fi
else
  fail "python3 not found"
fi

echo "== uv =="
if command -v uv >/dev/null 2>&1; then
  pass "uv $(uv --version | awk '{print $2}')"
else
  fail "uv not found — install from https://astral.sh/uv"
fi

echo "== lock state =="
if [ ! -f uv.lock ]; then
  fail "uv.lock missing — run: uv lock"
elif uv lock --check >/dev/null 2>&1; then
  pass "uv.lock is up to date"
else
  fail "uv.lock is stale — run: uv lock"
fi

echo "== environment variables =="
if [ -f .env.example ]; then
  missing=0
  while IFS='=' read -r key _; do
    case "$key" in
      ''|'#'*) continue ;;
    esac
    if [ -z "${!key:-}" ]; then
      missing=$((missing + 1))
    fi
  done < .env.example
  if [ "$missing" -gt 0 ]; then
    warn "$missing variable(s) from .env.example are unset in this shell (code defaults may cover them)"
  else
    pass "every variable in .env.example is set"
  fi
else
  warn ".env.example missing"
fi

echo "== database connectivity =="
db_url="${DATABASE_URL:-postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow}"
if uv run python -c "
from sqlalchemy import create_engine, text
engine = create_engine('${db_url}')
with engine.connect() as conn:
    conn.execute(text('SELECT 1'))
" >/dev/null 2>&1; then
  pass "database reachable"
else
  fail "database unreachable at configured DATABASE_URL — try: docker start odontoflow-db-1"
fi

echo "== alembic revision =="
current=$(uv run alembic -c alembic.ini current 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1)
head=$(uv run alembic -c alembic.ini heads 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1)
if [ -z "$head" ]; then
  fail "could not resolve migration head (is alembic configured?)"
elif [ "$current" = "$head" ]; then
  pass "database at head ($head)"
else
  warn "database at '${current:-<none>}', repo head is '$head' — run: uv run alembic upgrade head"
fi

echo "== supabase mcp =="
if [ -f .mcp.json ] || [ -f .codex/config.toml ]; then
  if grep -qE '"(Bearer|Authorization)"[^}]*[A-Za-z0-9_-]{20,}' .mcp.json 2>/dev/null; then
    fail ".mcp.json appears to contain a literal secret — use \${VAR} indirection instead"
  elif [ -n "${SUPABASE_ACCESS_TOKEN:-}" ]; then
    pass "SUPABASE_ACCESS_TOKEN is set"
  else
    warn "SUPABASE_ACCESS_TOKEN not set — supabase-core MCP server will fail to authenticate"
  fi
else
  warn "no .mcp.json / .codex/config.toml found — supabase MCP not configured"
fi

echo "== n8n mcp =="
if [ -f .mcp.json ] || [ -f .codex/config.toml ]; then
  if [ -n "${N8N_MCP_TOKEN:-}" ]; then
    pass "N8N_MCP_TOKEN is set"
  else
    warn "N8N_MCP_TOKEN not set — n8n MCP server will fail to authenticate"
  fi
else
  warn "no .mcp.json / .codex/config.toml found — n8n MCP not configured"
fi

echo "== gcloud =="
if command -v gcloud >/dev/null 2>&1; then
  if gcloud config configurations list --format="value(name)" 2>/dev/null | grep -qx "odontoflow-dev"; then
    active=$(gcloud config configurations list --format="value(name)" --filter="is_active=true" 2>/dev/null)
    if [ "$active" = "odontoflow-dev" ]; then
      pass "gcloud config 'odontoflow-dev' active"
    else
      warn "gcloud config 'odontoflow-dev' exists but is not active (active: ${active:-none}) — direnv sets this automatically"
    fi
  else
    warn "gcloud config 'odontoflow-dev' not found — run: gcloud config configurations create odontoflow-dev"
  fi
else
  warn "gcloud CLI not found"
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "doctor: OK"
else
  echo "doctor: FAILED"
fi
exit "$FAILED"

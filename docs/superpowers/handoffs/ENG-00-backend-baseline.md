# ENG-00 — Backend Engineering Baseline: handoff

Repository normalization only — no app architecture changes.

## What changed

1. **Secret fix (blocker found during planning):** `.mcp.json`'s n8n bearer
   token and `../.env.mcp.local`'s `N8N_MCP_TOKEN` were both missing their
   leading `e` (truncated `eyJ...` → `yJ...`) — this was the cause of the
   n8n MCP `AUTH_HEADER_REJECTED (HTTP 401)` seen at session start. Fixed
   both, and replaced the literal token in `.mcp.json` with `${N8N_MCP_TOKEN}`
   indirection (it was previously a plaintext credential on disk, wrapped in
   `${...}` by mistake). `.mcp.json` and `.codex/config.toml` are now
   committed since neither holds a literal secret.
2. **Config ownership:** `.env` renamed to `.env.local` (canonical dev
   secrets file); added repo-root `.envrc` sourcing it; `.gitignore` +=
   `.direnv/`. Settings loading in `app/config.py` / `sales_agent/config.py`
   is unchanged — still process-env only, no dotenv loader added.
3. **uv adoption:** `pyproject.toml` now uses `[dependency-groups].dev`
   (pytest, httpx, pyyaml, ruff); `agent` extra kept as-is. `uv.lock`
   generated from the already-declared dependency ranges — no version
   upgrades beyond what `>=` already allowed. `.python-version` (3.12)
   and `requires-python` (>=3.12) already agreed.
4. **Ruff:** conservative `[tool.ruff]` baseline added (py312, line-length
   100, `E`/`F`/`I` selected, alembic/versions excluded). No autofix run.
   `ruff check .` currently reports 130 findings (mostly import-order in
   `tests/*`, pre-existing, unfixed by design). `ruff format --check`
   reports 88 files would reformat — codebase never had a formatter
   applied; not run repo-wide per scope.
5. **`.env.example`:** added `ERP_ANONYMOUS_COMPAT` and the full
   `SALES_AGENT_*` contract. Every env var actually read by `app/config.py`,
   `app/run.py`, and `sales_agent/config.py` is now represented.
6. **New scripts:** `scripts/platform/doctor.sh` (env/DB/alembic/MCP/gcloud
   checks, never prints secret values) and `scripts/db/status.sh` (DB
   target with password redacted, alembic current vs. head).
7. **Docker:** added `Dockerfile` (uv 0.11.16 pinned, non-root, no
   `.venv`/dotenv baked in) and `.dockerignore`. `docker-compose.yml`
   untouched — it only ever defined local Postgres.
8. **Docs:** `README.md` / `DEVELOPMENT.md` updated to the uv workflow.
   Uses `uv run python -m pytest`, not the `pytest` console-script —
   the latter does not add cwd to `sys.path`, which broke
   `tests/test_bootstrap_n8n_lab.py` / `tests/test_seed_reception_demo.py`
   (`from scripts.* import ...`). This is noted so nobody "fixes" it back.

## Skipped

- **Nested `odontoflow-backend/` repair** — condition not met. No such
  directory exists in this repo; nothing to move or delete.

## Not done / follow-ups for the user

- **Rotate the n8n MCP bearer token.** It sat in plaintext in `.mcp.json`
  on disk (untracked, but unprotected) — rotate it in n8n once convenient.
- Three env vars in the old `.env` (now `.env.local`) are not consumed by
  any backend code: `SUPABASE_DATABASE_URL_SESSION`, `GEMINI_API_KEY`,
  `GOOGLE_OAUTH_CLIENT_ID`. Left in place (never delete unknown content)
  but not added to `.env.example`. Worth confirming they're still needed.
- Stray exited container `odontoflow-backend-db-1` (not `odontoflow-db-1`)
  exists from a past `docker compose up` run inside this directory —
  cosmetic, safe to `docker rm` whenever.
- `ruff check .` / `ruff format --check` are not clean (see §4). Left for
  a dedicated lint-cleanup task, not this one.

## Verification performed

`uv lock --check`, `uv sync --locked --all-extras`, import smoke test,
settings-load smoke test (both `app` and `sales_agent`), `ruff check .`
(report only), `ruff format --check .` (report only), `git diff --check`,
manual secret-pattern scan of every new/modified file, focused test slice
(75 passed), full suite (`uv run python -m pytest -q`: **527 passed**),
`docker build` + container import smoke test (image discarded after),
`scripts/platform/doctor.sh` (OK once DB started and MCP tokens exported),
`scripts/db/status.sh` (DB at head `0018`).

## Commits (this session)

1. `fix: repair n8n MCP bearer token and remove literal secret from .mcp.json`
2. `build: adopt uv as the canonical dependency runner`
3. `docs: complete .env.example with every env var the backend consumes`
4. `feat: add scripts/platform/doctor.sh and scripts/db/status.sh`
5. `feat: add a production Dockerfile and .dockerignore`
6. `docs: update local-dev instructions for the uv workflow`

## Pass check

```bash
uv sync --locked
./scripts/platform/doctor.sh
```

Both succeed on a clean checkout with `odontoflow-db-1` started, no
machine-specific secrets required beyond what `doctor.sh` warns about
(Supabase/n8n MCP tokens, if MCP use is desired).

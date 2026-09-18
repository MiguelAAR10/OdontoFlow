# CLAUDE.md — OdontoFlow Backend entry point

Claude Code does not auto-load `AGENTS.md`. The canonical backend engineering
contract is imported below, not duplicated — edit `AGENTS.md`, not this file.

@AGENTS.md

## Worker contract

You are a **backend implementation worker**. Product intent, activity selection,
and the final handoff belong to `../odontoflow-planning`; do not re-plan the
product here or start a different activity.

- Work only the SubCard you were dispatched with, inside its declared
  `write_ownership`. One writer per overlapping write surface.
- Load only the skills your lane names in
  `../odontoflow-planning/orchestration/skill-matrix.yaml`. They resolve from
  this repo's own `.claude/skills/` — nothing is inherited from planning.
  Backend lane: `api-and-interface-design`, `postgres-best-practices`,
  `security-and-hardening`, `test-driven-development`,
  `verification-before-completion`. Sales Agent surface adds
  `odontoflow-engineering`, `langchain-fundamentals`, `langgraph-persistence`,
  `context-engineering`.
- Tests are TDD-first and run against real PostgreSQL. Never run concurrent
  pytest processes — the test database is shared. Use `python -m pytest`.
- Respect `protected_surfaces` in
  `../odontoflow-planning/orchestration/project.yaml`. If your task requires
  touching one, stop and report the conflict.
- Preserve pre-existing dirty files you were not asked to change.
- Report back with files changed, tests run and their output, and what you did
  not do. Evidence before assertions.

"""Run scenarios k times on isolated clones and report ``pass^k``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Sequence

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from evals.checks import GUARDS, Check, World, resolve_conversation
from evals.harness import EvalDatabase, new_world, run_conversation
from integrations.sandbox.sender import SandboxSenderError


@dataclass
class TrialResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ScenarioResult:
    key: str
    title: str
    trials: list[TrialResult]

    @property
    def passes(self) -> int:
        return sum(1 for trial in self.trials if trial.passed)

    @property
    def pass_k(self) -> bool:
        """A scenario passes only if every one of its k repetitions passed."""
        return bool(self.trials) and self.passes == len(self.trials)


@dataclass
class SuiteResult:
    mode: str
    k: int
    model: str
    anchor: str
    scenarios: list[ScenarioResult]

    @property
    def pass_k(self) -> float:
        """Fraction of scenarios that passed all k repetitions."""
        if not self.scenarios:
            return 0.0
        return sum(1 for row in self.scenarios if row.pass_k) / len(self.scenarios)

    @property
    def exit_code(self) -> int:
        return 0 if self.scenarios and all(row.pass_k for row in self.scenarios) else 1

    def report(self) -> str:
        lines = [f"B5a evals — modo={self.mode} modelo={self.model} k={self.k} ancla={self.anchor}"]
        for row in self.scenarios:
            verdict = "PASS" if row.pass_k else "FAIL"
            lines.append(f"{row.key}: {row.passes}/{self.k} pass^{self.k}={verdict}  ({row.title})")
            for number, trial in enumerate(row.trials, start=1):
                if trial.error:
                    lines.append(f"  - intento {number}: error {trial.error}")
                for failure in trial.failures:
                    lines.append(f"  - intento {number}: falló «{failure}»")
        passed = sum(1 for row in self.scenarios if row.pass_k)
        lines.append(
            f"pass^{self.k}: {passed}/{len(self.scenarios)} escenarios ({self.pass_k:.2f})"
        )
        return "\n".join(lines)

    def baseline(self) -> dict[str, Any]:
        return {
            "card": "B5a",
            "mode": self.mode,
            "k": self.k,
            "model": self.model,
            "anchor": self.anchor,
            "pass_k": self.pass_k,
            "scenarios": [
                {"key": row.key, "title": row.title, "passes": row.passes, "pass_k": row.pass_k}
                for row in self.scenarios
            ],
        }


def _evaluate(checks: Sequence[Check], maker: sessionmaker, world: World) -> list[str]:
    failures: list[str] = []
    with maker() as session:
        resolve_conversation(session, world)
        for check in checks:
            try:
                ok = bool(check.fn(session, world))
            except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
                session.rollback()
                failures.append(f"{check.description} ({type(exc).__name__})")
                continue
            if not ok:
                failures.append(check.description)
    return failures


def run_trial(
    database: EvalDatabase,
    scenario: Any,
    *,
    model_factory: Callable[[], Any | None],
    agent_settings: Any,
) -> TrialResult:
    with database.trial() as (canonical_url, memory_url):
        engine = create_engine(canonical_url, pool_pre_ping=True)
        try:
            maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
            world = new_world(database, scenario)
            error = None
            try:
                run_conversation(
                    database,
                    scenario,
                    world,
                    maker=maker,
                    memory_url=memory_url,
                    model=model_factory(),
                    agent_settings=agent_settings,
                )
            except SandboxSenderError as exc:
                # Sender messages are content-free by contract (path + HTTP status).
                error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - a crashed turn is a failed trial
                error = type(exc).__name__
            failures = _evaluate((*GUARDS, *scenario.checks), maker, world)
        finally:
            engine.dispose()
    return TrialResult(passed=error is None and not failures, failures=failures, error=error)


def run_suite(
    scenarios: Sequence[Any],
    *,
    k: int,
    mode: str,
    model_name: str,
    anchor: date,
    model_factory: Callable[[], Any | None],
    agent_settings: Any,
    server_url: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> SuiteResult:
    results: list[ScenarioResult] = []
    with EvalDatabase(anchor=anchor, server_url=server_url) as database:
        for scenario in scenarios:
            trials = []
            for number in range(1, k + 1):
                trial = run_trial(
                    database,
                    scenario,
                    model_factory=model_factory,
                    agent_settings=agent_settings,
                )
                trials.append(trial)
                if progress is not None:
                    progress(
                        f"{scenario.key} intento {number}/{k}: {'ok' if trial.passed else 'FALLÓ'}"
                    )
            results.append(ScenarioResult(key=scenario.key, title=scenario.title, trials=trials))
    return SuiteResult(
        mode=mode, k=k, model=model_name, anchor=anchor.isoformat(), scenarios=results
    )


__all__ = ["ScenarioResult", "SuiteResult", "TrialResult", "run_suite", "run_trial"]

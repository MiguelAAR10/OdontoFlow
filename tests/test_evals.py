"""B5a — the early eval harness judges final database state with pass^k."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from conftest import TEST_DATABASE_URL
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_BASELINE = REPO_ROOT / "evals" / "baselines" / "fake-pass3.json"

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("langchain") is None or importlib.util.find_spec("langgraph") is None,
    reason="The eval harness drives the optional sales-agent runtime",
)


def _leftover_eval_databases() -> list[str]:
    server = create_engine(
        make_url(TEST_DATABASE_URL).set(database="odontoflow"), isolation_level="AUTOCOMMIT"
    )
    try:
        with server.connect() as connection:
            return list(
                connection.execute(
                    text("SELECT datname FROM pg_database WHERE datname LIKE 'odontoflow_eval_%'")
                ).scalars()
            )
    finally:
        server.dispose()


def test_pass_k_requires_every_repetition_to_pass():
    from evals.runner import ScenarioResult, SuiteResult, TrialResult

    flaky = ScenarioResult(
        key="flaky",
        title="flaky",
        trials=[TrialResult(passed=True), TrialResult(passed=True), TrialResult(passed=False)],
    )
    steady = ScenarioResult(
        key="steady",
        title="steady",
        trials=[TrialResult(passed=True) for _ in range(3)],
    )

    assert flaky.passes == 2
    assert flaky.pass_k is False
    assert steady.pass_k is True
    suite = SuiteResult(
        mode="fake", k=3, model="fake", anchor="2026-10-01", scenarios=[flaky, steady]
    )
    assert suite.pass_k == 0.5
    assert suite.exit_code == 1
    assert (
        SuiteResult(
            mode="fake", k=3, model="fake", anchor="2026-10-01", scenarios=[steady]
        ).exit_code
        == 0
    )


def test_five_reception_scenarios_pass_pass3_in_fake_mode(tmp_path, capsys):
    from evals.__main__ import main
    from evals.scenarios import SCENARIOS

    assert [scenario.key for scenario in SCENARIOS] == [
        "agendar_limpieza",
        "consulta_precio_horario",
        "reprogramar_cita",
        "cancelar_cita",
        "pedir_humano",
    ]
    baseline = tmp_path / "baseline.json"

    exit_code = main(["--mode", "fake", "--k", "3", "--write-baseline", str(baseline)])

    report = capsys.readouterr().out
    assert exit_code == 0, report
    for scenario in SCENARIOS:
        assert f"{scenario.key}: 3/3 pass^3=PASS" in report
    written = json.loads(baseline.read_text())
    assert written["mode"] == "fake"
    assert written["k"] == 3
    assert written["pass_k"] == 1.0
    assert [row["key"] for row in written["scenarios"]] == [s.key for s in SCENARIOS]
    assert all(row["passes"] == 3 and row["pass_k"] is True for row in written["scenarios"])
    # The committed baseline is the same fake-mode result (anchor aside).
    committed = json.loads(COMMITTED_BASELINE.read_text())
    assert {key: committed[key] for key in ("mode", "k", "model", "pass_k", "scenarios")} == {
        key: written[key] for key in ("mode", "k", "model", "pass_k", "scenarios")
    }
    # Every throwaway template/trial/memory database was dropped.
    assert _leftover_eval_databases() == []


def test_runner_fails_when_a_state_assertion_fails(capsys):
    from evals.__main__ import main
    from evals.checks import Check, count
    from evals.scenarios import SCENARIOS

    booking = next(s for s in SCENARIOS if s.key == "agendar_limpieza")
    # Deliberately wrong: the booking conversation creates exactly one proposal.
    wrong = booking.with_checks(
        "agendar_limpieza_mal",
        [
            Check(
                "2 propuestas de cita pendientes",
                lambda session, world: (
                    count(
                        session,
                        "SELECT count(*) FROM appointment_proposals WHERE status = 'pending'",
                    )
                    == 2
                ),
            )
        ],
    )

    def _explodes(session, world):
        raise RuntimeError("boom")

    raising = booking.with_checks(
        "agendar_limpieza_excepcion", [Check("check que explota", _explodes)]
    )

    exit_code = main(["--mode", "fake", "--k", "1"], scenarios=[wrong, raising])

    report = capsys.readouterr().out
    assert exit_code == 1
    assert "agendar_limpieza_mal: 0/1 pass^1=FAIL" in report
    assert "2 propuestas de cita pendientes" in report
    assert "agendar_limpieza_excepcion: 0/1 pass^1=FAIL" in report
    assert "check que explota" in report and "RuntimeError" in report
    assert _leftover_eval_databases() == []


def test_real_mode_without_a_key_exits_before_touching_the_database(monkeypatch, capsys):
    from evals.__main__ import main

    monkeypatch.setattr("evals.harness.EvalDatabase.__enter__", _forbidden)

    exit_code = main(["--mode", "real", "--k", "1"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "REAL_MODEL_UNAVAILABLE" in captured.err


def _forbidden(self):  # pragma: no cover - only runs if the guard is broken
    raise AssertionError("real mode without a key must not create a database")


def test_a_crashed_turn_fails_the_trial_but_checks_still_see_its_conversation():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from evals.runner import run_suite
    from evals.scenarios import SCENARIOS
    from sales_agent.config import AgentSettings

    class PlainTextModel(GenericFakeChatModel):
        """Answers without the structured response: the turn fails with HTTP 502."""

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            return self

    scenario = next(s for s in SCENARIOS if s.key == "consulta_precio_horario")
    result = run_suite(
        [scenario],
        k=1,
        mode="fake",
        model_name="plain-text",
        anchor=__import__("datetime").date(2026, 10, 1),
        model_factory=lambda: PlainTextModel(messages=iter([AIMessage(content="Hola")])),
        agent_settings=AgentSettings(
            backend_base_url="http://127.0.0.1:8000",
            backend_credential=None,
            agent_database_url="postgresql+psycopg://odontoflow@127.0.0.1:5434/odontoflow_eval_memory",
            model="plain-text",
            recursion_limit=12,
            request_timeout_seconds=10.0,
        ),
    )

    trial = result.scenarios[0].trials[0]
    assert result.exit_code == 1
    assert trial.passed is False
    assert trial.error is not None and trial.error.startswith("SandboxSenderTransportError")
    # The inbound message reached the canonical conversation: state checks judge it.
    assert "ninguna propuesta de cita en la conversación" not in trial.failures
    assert "0 derivación(es) a recepción pendiente(s)" not in trial.failures
    assert "conversación en estado 'open'" not in trial.failures
    # Only the reply is missing.
    assert trial.failures == ["una respuesta saliente por mensaje del paciente"]


def test_building_the_eval_database_leaves_log_capture_working(caplog):
    """The harness migrates in-process; alembic's ``fileConfig`` replaces root handlers."""
    import logging
    from datetime import date

    from evals.harness import EvalDatabase

    caplog.set_level(logging.WARNING, logger="sales_agent.diagnostics")
    diagnostics = logging.getLogger("sales_agent.diagnostics")

    with EvalDatabase(anchor=date(2026, 10, 1)):
        pass
    diagnostics.warning("after the eval database")

    assert [
        record.getMessage() for record in caplog.records if record.name == "sales_agent.diagnostics"
    ] == ["after the eval database"]

"""Tests for read-only diagnostic agents."""

import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

from appmonitor import LLMBudget, RunClient, RunSpec
from appmonitor.agents import (
    DiagnosticPipeline,
    SQLiteDiagnosticStore,
    build_diagnostic_context,
)
from appmonitor.openrouter import (
    ChatMessage,
    LLMUsage,
    ModelRoutingConstraints,
    StructuredCompletion,
)

_MAX_TEST_LINE_CHARS = 80


class FakeStructuredClient:
    """Return fixed structured agent outputs."""

    def __init__(self, outputs: list[dict[str, object]]) -> None:
        """Store outputs and captured prompts."""
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def complete_structured(  # noqa: PLR0913 - implements the production protocol
        self,
        *,
        task: str,
        messages: tuple[ChatMessage, ...],
        schema_name: str,
        schema: dict[str, object],
        budget: LLMBudget,
        min_context_tokens: int = 8_000,
        max_output_tokens: int = 1_000,
        max_attempts: int = 1,
        routing: ModelRoutingConstraints | None = None,
    ) -> StructuredCompletion:
        """Capture bounded call metadata and return the next output."""
        del schema, min_context_tokens, max_attempts, routing
        budget.begin_call(0)
        budget.finish_call(0, 0)
        self.calls.append(
            {
                "task": task,
                "messages": messages,
                "schema_name": schema_name,
                "max_output_tokens": max_output_tokens,
            },
        )
        return StructuredCompletion(
            call_id=f"call-{len(self.calls)}",
            model="fake/model",
            data=self.outputs.pop(0),
            usage=LLMUsage(10, 5, 0),
            latency_seconds=0.01,
        )


def _critic_output(*, investigate: bool = False) -> dict[str, object]:
    return {
        "summary": (
            "The run completed but requires review." if investigate else "The run is healthy."
        ),
        "goal_alignment": "unknown" if investigate else "satisfied",
        "findings": (
            [
                {
                    "category": "correctness",
                    "severity": "high",
                    "summary": "The target exited with an error.",
                    "evidence": ["exit_code=1"],
                    "recommendation": "Investigate the captured traceback.",
                    "confidence": 0.95,
                },
            ]
            if investigate
            else []
        ),
        "needs_investigation": investigate,
        "confidence": 0.9,
    }


def _incident_output() -> dict[str, object]:
    return {
        "classification": "runtime_error",
        "root_cause": "The target raised RuntimeError.",
        "evidence": ["stderr contains RuntimeError: broken"],
        "suspected_files": [],
        "reproduction_steps": ["Run the monitored command."],
        "priority": "high",
        "confidence": 0.93,
    }


def test_diagnostic_context_is_bounded_and_redacts_secrets(tmp_path: Path) -> None:
    """Agents receive summarized facts without credentials or unbounded logs."""
    secret = "sk-or-v1-super-secret"  # noqa: S105 - synthetic redaction fixture
    run = RunClient().execute(
        RunSpec(
            repository=tmp_path,
            command=[
                sys.executable,
                "-c",
                f"print('token={secret}'); print('x' * 1000)",
            ],
        ),
    )

    context = build_diagnostic_context(
        run,
        max_log_lines=1,
        max_line_chars=_MAX_TEST_LINE_CHARS,
    )
    serialized = json.dumps(context)

    assert secret not in serialized
    assert "[REDACTED]" in serialized
    runtime = cast("dict[str, object]", context["runtime"])
    stdout = runtime["stdout"]
    assert isinstance(stdout, list)
    assert len(stdout) == 1
    assert len(stdout[0]) <= _MAX_TEST_LINE_CHARS


def test_pipeline_skips_incident_for_healthy_run_and_persists(tmp_path: Path) -> None:
    """A healthy run costs one critic call and has no incident analysis."""
    run = RunClient().execute(
        RunSpec(repository=tmp_path, command=[sys.executable, "-c", "print('ok')"]),
    )
    client = FakeStructuredClient([_critic_output()])
    store = SQLiteDiagnosticStore(tmp_path / ".appmonitor" / "runs.sqlite3")

    result = DiagnosticPipeline(client=client, store=store).analyze(
        run,
        budget=LLMBudget(max_calls=2, max_cost_usd=0.01),
    )

    assert result.assessment.summary == "The run is healthy."
    assert result.incident is None
    assert [call["task"] for call in client.calls] == ["run_critic"]
    with sqlite3.connect(store.database) as connection:
        row = connection.execute(
            "SELECT assessment_json, incident_json FROM run_diagnostics WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert json.loads(row[0])["goal_alignment"] == "satisfied"
    assert row[1] is None


def test_pipeline_invokes_incident_agent_for_failed_run(tmp_path: Path) -> None:
    """Failure facts trigger a separate structured incident analysis."""
    run = RunClient().execute(
        RunSpec(
            repository=tmp_path,
            command=[sys.executable, "-c", "raise RuntimeError('broken')"],
        ),
    )
    client = FakeStructuredClient([_critic_output(investigate=True), _incident_output()])

    result = DiagnosticPipeline(client=client).analyze(
        run,
        budget=LLMBudget(max_calls=2, max_cost_usd=0.01),
    )

    assert result.incident is not None
    assert result.incident.classification == "runtime_error"
    assert result.incident.priority == "high"
    assert [call["task"] for call in client.calls] == ["run_critic", "incident_analyst"]

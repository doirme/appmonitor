"""Tests for optional in-process Python instrumentation."""

from pathlib import Path

import pytest

from appmonitor.instrumentation import (
    CallReference,
    InMemoryCallRecorder,
    OutputArtifact,
    ResourceBudget,
    SQLiteInstrumentationStore,
    monitored,
)

_EXPECTED_SUM = 5


def test_monitored_records_bound_arguments_return_and_timing() -> None:
    """A successful call retains bounded structural observations."""
    recorder = InMemoryCallRecorder()

    @monitored(goal="add values", recorder=recorder)
    def add(left: int, right: int = 1) -> int:
        return left + right

    result = add(2, right=3)

    assert result == _EXPECTED_SUM
    observation = recorder.records[0]
    assert observation.function.endswith("add")
    assert observation.goal == "add values"
    assert observation.arguments == {"left": "2", "right": "3"}
    assert observation.outcome == "returned"
    assert observation.return_type == "int"
    assert observation.return_sha256 is not None
    assert observation.duration_seconds >= 0


def test_monitored_redacts_secret_arguments_and_reraises() -> None:
    """Instrumentation records failures without retaining obvious secrets."""
    recorder = InMemoryCallRecorder()

    @monitored(goal="fail safely", recorder=recorder)
    def fail(api_key: str) -> None:
        message = f"provider rejected {api_key}"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="provider rejected"):
        fail("sk-or-sensitive-value")

    observation = recorder.records[0]
    assert observation.arguments == {"api_key": "<redacted>"}
    assert observation.outcome == "raised"
    assert observation.exception_type == "RuntimeError"
    assert "sensitive-value" not in (observation.exception_message or "")


def test_monitored_evaluates_artifacts_and_resource_budget(tmp_path: Path) -> None:
    """Required outputs and resource bounds become deterministic checks."""
    recorder = InMemoryCallRecorder()

    @monitored(
        goal="write result",
        outputs=(OutputArtifact("outputs/*.txt"), OutputArtifact("missing/*.json")),
        budget=ResourceBudget(max_runtime_seconds=10, max_memory_delta_mb=100),
        repository=tmp_path,
        recorder=recorder,
    )
    def write_result() -> None:
        output = tmp_path / "outputs" / "result.txt"
        output.parent.mkdir()
        output.write_text("ready", encoding="utf-8")

    write_result()

    observation = recorder.records[0]
    assert observation.artifacts.created[0].path == "outputs/result.txt"
    assert [(check.pattern, check.passed) for check in observation.output_checks] == [
        ("outputs/*.txt", True),
        ("missing/*.json", False),
    ]
    assert all(check.passed for check in observation.budget_checks)


def test_reference_comparison_detects_stable_return() -> None:
    """A prior observation can act as an explicit comparison baseline."""
    baseline_recorder = InMemoryCallRecorder()

    @monitored(goal="stable", recorder=baseline_recorder)
    def baseline() -> str:
        return "same"

    baseline()
    reference = CallReference.from_observation(baseline_recorder.records[0])
    recorder = InMemoryCallRecorder()

    @monitored(goal="stable", recorder=recorder, reference=reference)
    def candidate() -> str:
        return "same"

    candidate()

    comparison = recorder.records[0].reference_comparison
    assert comparison is not None
    assert comparison.return_matches is True
    assert comparison.duration_ratio is not None


def test_sqlite_instrumentation_store_round_trips_records(tmp_path: Path) -> None:
    """Call observations can be retained durably without pickled values."""
    store = SQLiteInstrumentationStore(tmp_path / "calls.sqlite3")

    @monitored(goal="persist", recorder=store)
    def identity(value: int) -> int:
        return value

    identity(7)

    records = store.list_records()
    assert len(records) == 1
    assert records[0].goal == "persist"
    assert records[0].arguments == {"value": "7"}

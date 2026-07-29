"""Tests for deterministic run orchestration."""

import sys
from pathlib import Path

import pytest

from appmonitor import RunClient, RunSpec, SQLiteRunStore
from appmonitor.execution import RunOutcome
from appmonitor.states import RunState


def test_client_executes_and_persists_complete_lifecycle(tmp_path: Path) -> None:
    """The client connects validation, execution, state transitions, and storage."""
    database = tmp_path / ".appmonitor" / "runs.sqlite3"
    client = RunClient(store=SQLiteRunStore(database))
    spec = RunSpec(repository=tmp_path, command=[sys.executable, "-c", "print('managed')"])

    result = client.execute(spec)

    assert result.report.outcome is RunOutcome.SUCCEEDED
    assert [transition.current for transition in result.transitions] == [
        RunState.REPOSITORY_PREPARED,
        RunState.ANALYZED,
        RunState.ENVIRONMENT_READY,
        RunState.RUNNING,
        RunState.SUCCEEDED,
        RunState.REVIEWED,
        RunState.REPORTED,
    ]
    stored = SQLiteRunStore(database).load(result.run_id)
    assert stored["run_id"] == result.run_id
    assert stored["transitions"][-1]["current"] == "reported"
    assert stored["stdout"][0]["message"] == "managed"


@pytest.mark.parametrize(
    ("command", "timeout_seconds", "expected_outcome", "expected_state"),
    [
        (
            [sys.executable, "-c", "raise RuntimeError('broken')"],
            None,
            RunOutcome.FAILED,
            RunState.FAILED,
        ),
        (
            [sys.executable, "-c", "import time; time.sleep(10)"],
            0.1,
            RunOutcome.TIMED_OUT,
            RunState.TIMED_OUT,
        ),
    ],
)
def test_client_maps_process_outcome_to_terminal_state(
    tmp_path: Path,
    command: list[str],
    timeout_seconds: float | None,
    expected_outcome: RunOutcome,
    expected_state: RunState,
) -> None:
    """Failure and timeout facts deterministically select lifecycle states."""
    result = RunClient().execute(
        RunSpec(
            repository=tmp_path,
            command=command,
            timeout_seconds=timeout_seconds,
        ),
    )

    assert result.report.outcome is expected_outcome
    assert expected_state in {transition.current for transition in result.transitions}
    assert result.transitions[-1].current is RunState.REPORTED
    assert (tmp_path / ".appmonitor" / "runs.sqlite3").is_file()


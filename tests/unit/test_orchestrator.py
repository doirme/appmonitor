"""Tests for deterministic run orchestration."""

import sys
from pathlib import Path
from typing import cast

import pytest

from appmonitor import GitAutomationError, RunClient, RunSpec, SQLiteRunStore
from appmonitor.analysis import StaticAnalyzer
from appmonitor.execution import RunOutcome
from appmonitor.repository import (
    CommandResult,
    EnvironmentPreparationError,
    EnvironmentPreparer,
)
from appmonitor.states import RunState


class FixedCommandRunner:
    """Return one fixed infrastructure command result."""

    def __init__(self, result: CommandResult) -> None:
        """Store the result returned for every invocation."""
        self.result = result

    def run(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Return the configured result."""
        del command, cwd
        return self.result


class FakeRemoteGit:
    """Record remote startup checks without invoking Git."""

    def __init__(self, *, error: GitAutomationError | None = None) -> None:
        """Store an optional preflight failure."""
        self.error = error
        self.calls: list[tuple[Path, str, str]] = []

    def preflight(self, repository: Path, *, run_id: str, remote: str) -> None:
        """Record or reject one remote preflight."""
        self.calls.append((repository, run_id, remote))
        if self.error:
            raise self.error


def test_client_executes_and_persists_complete_lifecycle(tmp_path: Path) -> None:
    """The client connects validation, execution, state transitions, and storage."""
    database = tmp_path / ".appmonitor" / "runs.sqlite3"
    (tmp_path / "module.py").write_text("def indexed():\n    return 1\n", encoding="utf-8")
    preparer = EnvironmentPreparer(runner=FixedCommandRunner(CommandResult(0, "ready", "")))
    client = RunClient(
        store=SQLiteRunStore(database),
        environment_preparer=preparer,
        static_analyzer=StaticAnalyzer(run_tools=False),
    )
    spec = RunSpec(
        repository=tmp_path,
        command=[sys.executable, "-c", "print('managed')"],
        sync_environment=True,
        analyze_repository=True,
    )

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
    assert stored["repository_facts"]["is_git_repository"] is False
    assert stored["environment_facts"]["uv_sync_performed"] is True
    assert stored["environment_facts"]["uv_sync_succeeded"] is True
    assert stored["analysis"]["symbols"][0]["qualified_name"] == "indexed"


def test_client_evaluates_and_persists_goal_contract(tmp_path: Path) -> None:
    """A run goal is included in both the result and durable report."""
    goal_file = tmp_path / "goal.yaml"
    goal_file.write_text(
        "version: 1\nprocess:\n  exit_code: 0\nevents:\n  stdout_contains: [managed]\n",
        encoding="utf-8",
    )
    database = tmp_path / ".appmonitor" / "runs.sqlite3"
    client = RunClient(store=SQLiteRunStore(database))

    result = client.execute(
        RunSpec(
            repository=tmp_path,
            command=[sys.executable, "-c", "print('managed')"],
            goal_file=goal_file,
        ),
    )

    assert result.goal_evaluation is not None
    assert result.goal_evaluation.overall == "passed"
    stored = SQLiteRunStore(database).load(result.run_id)
    goal = stored["goal"]
    assert goal is not None
    contract = cast("dict[str, object]", goal["contract"])
    evaluation = cast("dict[str, object]", goal["evaluation"])
    assert contract["version"] == 1
    assert evaluation["overall"] == "passed"


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


def test_client_stops_when_frozen_environment_sync_fails(tmp_path: Path) -> None:
    """An explicitly requested failed uv sync prevents target execution."""
    marker = tmp_path / "target-ran.txt"
    preparer = EnvironmentPreparer(
        runner=FixedCommandRunner(CommandResult(2, "", "lock mismatch")),
    )
    client = RunClient(environment_preparer=preparer)
    spec = RunSpec(
        repository=tmp_path,
        command=[sys.executable, "-c", f"open(r'{marker}', 'w').close()"],
        sync_environment=True,
    )

    with pytest.raises(EnvironmentPreparationError, match="lock mismatch"):
        client.execute(spec)

    assert not marker.exists()


def test_client_preflights_opt_in_remote_before_target_execution(tmp_path: Path) -> None:
    """Denied remote publication prevents the monitored command from starting."""
    marker = tmp_path / "target-ran.txt"
    remote = FakeRemoteGit(error=GitAutomationError("remote origin denied push access"))
    client = RunClient(remote_git=remote)

    with pytest.raises(GitAutomationError, match="denied push access"):
        client.execute(
            RunSpec(
                repository=tmp_path,
                command=[sys.executable, "-c", f"open(r'{marker}', 'w').close()"],
                git_remote="origin",
            ),
        )

    assert len(remote.calls) == 1
    assert remote.calls[0][0] == tmp_path
    assert remote.calls[0][2] == "origin"
    assert not marker.exists()


def test_client_skips_remote_preflight_in_default_local_mode(tmp_path: Path) -> None:
    """Local-only monitoring has no remote Git prerequisite."""
    remote = FakeRemoteGit()

    result = RunClient(remote_git=remote).execute(
        RunSpec(repository=tmp_path, command=[sys.executable, "-c", "pass"]),
    )

    assert result.git_remote is None
    assert remote.calls == []

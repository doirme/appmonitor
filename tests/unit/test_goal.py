"""Tests for deterministic goal contracts."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from appmonitor.artifacts import Artifact, ArtifactChanges
from appmonitor.execution import CapturedLine, ProcessMetrics, RunOutcome, RunReport
from appmonitor.goal import GoalContractError, GoalEvaluator, load_goal_contract


def _report(*, metrics: tuple[ProcessMetrics, ...] = ()) -> RunReport:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    return RunReport(
        command=("python", "target.py"),
        repository="repository",
        outcome=RunOutcome.SUCCEEDED,
        exit_code=0,
        timed_out=False,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        stdout=(CapturedLine(started, "training complete"),),
        stderr=(),
        metrics=metrics,
        artifacts=ArtifactChanges(
            created=(Artifact("results/summary.json", 20, 1, "digest"),),
        ),
    )


def test_load_and_evaluate_complete_goal(tmp_path: Path) -> None:
    """A valid contract evaluates process, artifacts, events, and resources."""
    goal_file = tmp_path / "goal.yaml"
    goal_file.write_text(
        """
version: 1
process:
  exit_code: 0
artifacts:
  required:
    - results/*.json
events:
  stdout_contains:
    - training complete
resources:
  max_runtime_seconds: 2
  max_peak_rss_mb: 64
""".lstrip(),
        encoding="utf-8",
    )
    started = datetime(2026, 1, 1, tzinfo=UTC)

    contract = load_goal_contract(goal_file)
    evaluation = GoalEvaluator().evaluate(
        contract,
        _report(metrics=(ProcessMetrics(started, 1024, 1.0, 1, 1),)),
    )

    assert contract.sha256
    assert evaluation.overall == "passed"
    assert {check.status for check in evaluation.checks} == {"passed"}


def test_goal_reports_failed_and_unavailable_checks(tmp_path: Path) -> None:
    """Failed observations and absent metrics remain distinct."""
    goal_file = tmp_path / "goal.yaml"
    goal_file.write_text(
        """
version: 1
artifacts:
  required: [missing/*.json]
resources:
  max_peak_rss_mb: 1
""".lstrip(),
        encoding="utf-8",
    )

    evaluation = GoalEvaluator().evaluate(load_goal_contract(goal_file), _report())

    assert evaluation.overall == "failed"
    assert [check.status for check in evaluation.checks] == ["failed", "unavailable"]


@pytest.mark.parametrize(
    "content",
    [
        "version: 2\n",
        "version: 1\nassertions:\n  expression: __import__('os').system('bad')\n",
        "version: 1\nprocess:\n  unknown: true\n",
    ],
)
def test_goal_rejects_unsupported_or_unsafe_schema(tmp_path: Path, content: str) -> None:
    """Only the documented data-only schema is accepted."""
    goal_file = tmp_path / "goal.yaml"
    goal_file.write_text(content, encoding="utf-8")

    with pytest.raises(GoalContractError):
        load_goal_contract(goal_file)

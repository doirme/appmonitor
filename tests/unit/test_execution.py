"""Tests for local deterministic execution."""

import sys
from pathlib import Path

from appmonitor import RunSpec
from appmonitor.execution import LocalExecutor, RunOutcome

_MAX_TIMEOUT_TEST_DURATION_SECONDS = 5


def test_executor_captures_streams_and_created_artifacts(tmp_path: Path) -> None:
    """A successful run captures both streams and filesystem changes."""
    script = tmp_path / "target.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "print('normal output')\n"
        "print('warning output', file=sys.stderr)\n"
        "Path('result.txt').write_text('result', encoding='utf-8')\n"
        "time.sleep(0.1)\n",
        encoding="utf-8",
    )
    spec = RunSpec(repository=tmp_path, command=[sys.executable, str(script)])

    report = LocalExecutor().execute(spec)

    assert report.outcome is RunOutcome.SUCCEEDED
    assert report.exit_code == 0
    assert [line.message for line in report.stdout] == ["normal output"]
    assert [line.message for line in report.stderr] == ["warning output"]
    assert {artifact.path for artifact in report.artifacts.created} == {"result.txt"}
    assert report.duration_seconds >= 0
    assert report.started_at <= report.finished_at
    assert report.metrics
    assert report.peak_rss_bytes > 0


def test_executor_reports_failure(tmp_path: Path) -> None:
    """A non-zero child exit is a failed run rather than an executor exception."""
    spec = RunSpec(
        repository=tmp_path,
        command=[sys.executable, "-c", "raise RuntimeError('broken')"],
    )

    report = LocalExecutor().execute(spec)

    assert report.outcome is RunOutcome.FAILED
    assert report.exit_code != 0
    assert any("RuntimeError: broken" in line.message for line in report.stderr)


def test_executor_enforces_timeout(tmp_path: Path) -> None:
    """A run exceeding its declared budget is terminated and reported."""
    spec = RunSpec(
        repository=tmp_path,
        command=[sys.executable, "-c", "import time; time.sleep(10)"],
        timeout_seconds=0.1,
    )

    report = LocalExecutor().execute(spec)

    assert report.outcome is RunOutcome.TIMED_OUT
    assert report.timed_out is True
    assert report.duration_seconds < _MAX_TIMEOUT_TEST_DURATION_SECONDS


def test_report_serializes_to_json(tmp_path: Path) -> None:
    """Reports expose a portable JSON representation for persistence."""
    spec = RunSpec(repository=tmp_path, command=[sys.executable, "-c", "print('ok')"])

    payload = LocalExecutor().execute(spec).to_json()

    assert '"outcome": "succeeded"' in payload
    assert '"message": "ok"' in payload

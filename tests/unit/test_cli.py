"""Tests for the command-line interface."""

import json
import sys
from pathlib import Path

import pytest

from appmonitor.cli import main


def test_run_command_emits_json_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run subcommand executes the target and prints a portable report."""
    exit_code = main(
        [
            "run",
            "--repo",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            "print('cli output')",
        ],
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["run_id"]
    assert payload["outcome"] == "succeeded"
    assert payload["stdout"][0]["message"] == "cli output"
    assert payload["transitions"][-1]["current"] == "reported"
    assert (tmp_path / ".appmonitor" / "runs.sqlite3").is_file()

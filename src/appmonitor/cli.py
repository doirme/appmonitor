"""AppMonitor command-line interface."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from appmonitor.models import RunSpec
from appmonitor.orchestrator import RunClient

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the AppMonitor CLI and return a process exit code."""
    parser = argparse.ArgumentParser(prog="appmonitor")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    run_parser = subparsers.add_parser("run", help="monitor a local command")
    run_parser.add_argument("--repo", type=Path, default=Path.cwd())
    run_parser.add_argument("--timeout", type=float)
    run_parser.add_argument("--sync-environment", action="store_true")
    run_parser.add_argument("--analyze", action="store_true")
    run_parser.add_argument("--goal", type=Path)
    run_parser.add_argument("--git-remote")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    spec = RunSpec(
        repository=arguments.repo,
        command=command,
        timeout_seconds=arguments.timeout,
        sync_environment=arguments.sync_environment,
        analyze_repository=arguments.analyze,
        goal_file=arguments.goal,
        git_remote=arguments.git_remote,
    )
    print(RunClient().execute(spec).to_json())  # noqa: T201
    return 0

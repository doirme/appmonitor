"""Tests for repository and uv environment reconstruction."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

from appmonitor.repository import (
    CommandResult,
    EnvironmentPreparer,
    RepositoryInspector,
)

if TYPE_CHECKING:
    from pathlib import Path


class StubRunner:
    """Return deterministic command results without starting subprocesses."""

    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        """Store results keyed by complete argument vector."""
        self.results = results
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Record and resolve a configured command."""
        self.calls.append((command, cwd))
        return self.results[command]


def test_repository_inspector_collects_git_and_uv_facts(tmp_path: Path) -> None:
    """Repository identity includes revision, worktree state, and lock digest."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    lock_content = b"version = 1\n"
    (tmp_path / "uv.lock").write_bytes(lock_content)
    runner = StubRunner(
        {
            ("git", "rev-parse", "--show-toplevel"): CommandResult(0, str(tmp_path), ""),
            ("git", "rev-parse", "HEAD"): CommandResult(0, "a" * 40, ""),
            ("git", "branch", "--show-current"): CommandResult(0, "main", ""),
            ("git", "status", "--porcelain"): CommandResult(0, " M file.py", ""),
        },
    )

    facts = RepositoryInspector(runner=runner).inspect(tmp_path)

    assert facts.is_git_repository is True
    assert facts.git_root == tmp_path
    assert facts.commit == "a" * 40
    assert facts.branch == "main"
    assert facts.dirty is True
    assert facts.has_pyproject is True
    assert facts.has_uv_lock is True
    assert facts.uv_lock_sha256 == sha256(lock_content).hexdigest()


def test_repository_inspector_supports_non_git_directories(tmp_path: Path) -> None:
    """Monitoring a plain directory does not require Git."""
    command = ("git", "rev-parse", "--show-toplevel")
    runner = StubRunner({command: CommandResult(128, "", "not a git repository")})

    facts = RepositoryInspector(runner=runner).inspect(tmp_path)

    assert facts.is_git_repository is False
    assert facts.git_root is None
    assert facts.commit is None
    assert facts.branch is None
    assert facts.dirty is None


def test_environment_preparer_runs_frozen_uv_sync(tmp_path: Path) -> None:
    """Environment preparation uses the reproducible frozen uv command."""
    command = ("uv", "sync", "--frozen")
    runner = StubRunner({command: CommandResult(0, "ready", "")})

    facts = EnvironmentPreparer(runner=runner).prepare(tmp_path)

    assert runner.calls == [(command, tmp_path)]
    assert facts.uv_sync_performed is True
    assert facts.uv_sync_succeeded is True
    assert facts.uv_sync_command == command

"""Repository identity and reproducible uv environment preparation."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

_COMMAND_NOT_FOUND_EXIT_CODE = 127


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Portable result of one infrastructure command."""

    exit_code: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Boundary for deterministic infrastructure command execution."""

    def run(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Execute an argument vector in an explicit working directory."""
        ...


class SubprocessCommandRunner:
    """Execute infrastructure commands without a shell."""

    def run(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Run a command and capture its decoded output."""
        try:
            completed = subprocess.run(  # noqa: S603 - command is an explicit argument vector
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as error:
            return CommandResult(127, "", str(error))
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    """Reproducibility facts for one local repository directory."""

    root: Path
    git_available: bool
    is_git_repository: bool
    git_root: Path | None
    commit: str | None
    branch: str | None
    dirty: bool | None
    has_pyproject: bool
    has_uv_lock: bool
    uv_lock_sha256: str | None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "root": str(self.root),
            "git_available": self.git_available,
            "is_git_repository": self.is_git_repository,
            "git_root": str(self.git_root) if self.git_root else None,
            "commit": self.commit,
            "branch": self.branch,
            "dirty": self.dirty,
            "has_pyproject": self.has_pyproject,
            "has_uv_lock": self.has_uv_lock,
            "uv_lock_sha256": self.uv_lock_sha256,
        }


@dataclass(frozen=True, slots=True)
class EnvironmentFacts:
    """Facts describing optional uv environment preparation."""

    python_executable: str
    uv_sync_performed: bool
    uv_sync_succeeded: bool | None
    uv_sync_command: tuple[str, ...] | None
    uv_sync_exit_code: int | None
    uv_sync_stdout: str
    uv_sync_stderr: str

    @classmethod
    def current(cls) -> EnvironmentFacts:
        """Describe the current interpreter without preparing an environment."""
        return cls(
            python_executable=sys.executable,
            uv_sync_performed=False,
            uv_sync_succeeded=None,
            uv_sync_command=None,
            uv_sync_exit_code=None,
            uv_sync_stdout="",
            uv_sync_stderr="",
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "python_executable": self.python_executable,
            "uv_sync_performed": self.uv_sync_performed,
            "uv_sync_succeeded": self.uv_sync_succeeded,
            "uv_sync_command": list(self.uv_sync_command) if self.uv_sync_command else None,
            "uv_sync_exit_code": self.uv_sync_exit_code,
            "uv_sync_stdout": self.uv_sync_stdout,
            "uv_sync_stderr": self.uv_sync_stderr,
        }


class RepositoryInspector:
    """Collect local Git, project, and lockfile identity without modification."""

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """Create an inspector with an injectable command boundary."""
        self._runner = runner or SubprocessCommandRunner()

    def inspect(self, repository: Path) -> RepositoryFacts:
        """Inspect one repository directory and return reproducibility facts."""
        root = repository.resolve()
        lockfile = root / "uv.lock"
        top_level = self._runner.run(("git", "rev-parse", "--show-toplevel"), cwd=root)
        git_available = top_level.exit_code != _COMMAND_NOT_FOUND_EXIT_CODE
        if top_level.exit_code != 0:
            return RepositoryFacts(
                root=root,
                git_available=git_available,
                is_git_repository=False,
                git_root=None,
                commit=None,
                branch=None,
                dirty=None,
                has_pyproject=(root / "pyproject.toml").is_file(),
                has_uv_lock=lockfile.is_file(),
                uv_lock_sha256=_hash_optional_file(lockfile),
            )
        commit = self._runner.run(("git", "rev-parse", "HEAD"), cwd=root)
        branch = self._runner.run(("git", "branch", "--show-current"), cwd=root)
        status = self._runner.run(("git", "status", "--porcelain"), cwd=root)
        return RepositoryFacts(
            root=root,
            git_available=git_available,
            is_git_repository=True,
            git_root=Path(top_level.stdout.strip()).resolve(),
            commit=_successful_output(commit),
            branch=_successful_output(branch),
            dirty=bool(status.stdout.strip()) if status.exit_code == 0 else None,
            has_pyproject=(root / "pyproject.toml").is_file(),
            has_uv_lock=lockfile.is_file(),
            uv_lock_sha256=_hash_optional_file(lockfile),
        )


class EnvironmentPreparer:
    """Prepare a project environment using a frozen uv lockfile."""

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """Create a preparer with an injectable command boundary."""
        self._runner = runner or SubprocessCommandRunner()

    def prepare(self, repository: Path) -> EnvironmentFacts:
        """Run `uv sync --frozen` and return all deterministic command facts."""
        command = ("uv", "sync", "--frozen")
        result = self._runner.run(command, cwd=repository.resolve())
        return EnvironmentFacts(
            python_executable=sys.executable,
            uv_sync_performed=True,
            uv_sync_succeeded=result.exit_code == 0,
            uv_sync_command=command,
            uv_sync_exit_code=result.exit_code,
            uv_sync_stdout=result.stdout,
            uv_sync_stderr=result.stderr,
        )


class EnvironmentPreparationError(RuntimeError):
    """Raised when an explicitly requested frozen uv sync fails."""


def _successful_output(result: CommandResult) -> str | None:
    """Return stripped stdout only for a successful non-empty command."""
    output = result.stdout.strip()
    return output if result.exit_code == 0 and output else None


def _hash_optional_file(path: Path) -> str | None:
    """Return a streaming SHA-256 digest when a file exists."""
    if not path.is_file():
        return None
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

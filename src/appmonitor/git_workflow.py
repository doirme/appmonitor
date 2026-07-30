"""Isolated Git worktrees and bounded local commits for verified maintenance."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol, cast

from appmonitor.repository import RepositoryInspector, SubprocessCommandRunner

if TYPE_CHECKING:
    from appmonitor.agents import DiagnosticResult
    from appmonitor.openrouter import LLMBudget
    from appmonitor.orchestrator import OrchestratedRun
    from appmonitor.patching import PatchPipelineResult
    from appmonitor.regression import RegressionTestResult
    from appmonitor.repository import CommandResult, CommandRunner

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRANCH_PREFIX = "appmonitor/"
_MAX_COMMIT_MESSAGE_CHARS = 200
_PORCELAIN_MIN_LINE_LENGTH = 4

MaintenanceStatus = Literal["committed", "rejected"]


class GitAutomationError(RuntimeError):
    """Raised before an unsafe or ambiguous Git operation."""


class RegressionWorkflow(Protocol):
    """Regression capability required inside a prepared worktree."""

    def generate(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
    ) -> RegressionTestResult:
        """Generate and prove one regression."""


class PatchWorkflow(Protocol):
    """Patch capability required inside a prepared worktree."""

    def execute(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        regression: RegressionTestResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
    ) -> PatchPipelineResult:
        """Apply and verify one bounded patch."""


@dataclass(frozen=True, slots=True)
class PreparedWorktree:
    """Detached worktree prepared from one exact clean base revision."""

    repository: Path
    path: Path
    branch: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class GitCommitResult:
    """Identity and exact path scope of one local maintenance commit."""

    branch: str
    commit: str
    base_commit: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitMaintenanceResult:
    """Complete V1 maintenance decision through an optional local commit."""

    status: MaintenanceStatus
    reason: str
    branch: str
    base_commit: str
    commit: str | None
    changed_paths: tuple[str, ...]
    regression: RegressionTestResult
    patch: PatchPipelineResult | None

    def to_dict(self) -> dict[str, object]:
        """Return a portable maintenance audit record."""
        return {
            "status": self.status,
            "reason": self.reason,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "commit": self.commit,
            "changed_paths": list(self.changed_paths),
            "regression": self.regression.to_dict(),
            "patch": self.patch.to_dict() if self.patch else None,
        }


_GIT_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_git_maintenance (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    commit_sha TEXT,
    changed_paths_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
"""


class SQLiteGitStore:
    """Persist one final Git maintenance decision against its source run."""

    def __init__(self, database: str | Path) -> None:
        """Initialize the Git maintenance table."""
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_GIT_STORE_SCHEMA)

    def save(self, run_id: str, result: GitMaintenanceResult) -> None:
        """Insert one immutable Git decision."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO run_git_maintenance (
                    run_id, status, reason, branch, base_commit,
                    commit_sha, changed_paths_json, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.status,
                    result.reason,
                    result.branch,
                    result.base_commit,
                    result.commit,
                    json.dumps(result.changed_paths),
                    json.dumps(result.to_dict(), sort_keys=True),
                ),
            )

    def load(self, run_id: str) -> dict[str, object]:
        """Load one portable Git decision or raise `KeyError`."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT result_json FROM run_git_maintenance WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return cast("dict[str, object]", json.loads(row[0]))

    def _connect(self) -> sqlite3.Connection:
        """Open one foreign-key-enforcing connection."""
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class GitWorktreeManager:
    """Create detached worktrees and commit only explicitly authorized paths."""

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """Use the no-shell subprocess boundary by default."""
        self._runner = runner or SubprocessCommandRunner()

    def prepare(self, repository: str | Path, *, run_id: str) -> PreparedWorktree:
        """Create a detached worktree from a clean current commit."""
        root = Path(repository).resolve()
        if not _RUN_ID.fullmatch(run_id):
            message = "run_id is not safe for a Git branch or worktree path"
            raise GitAutomationError(message)
        top_level = self._git(root, "rev-parse", "--show-toplevel")
        if top_level.exit_code != 0:
            message = "Git maintenance requires a valid Git repository"
            raise GitAutomationError(message)
        if _meaningful_status(self._git_required(root, "status", "--porcelain=v1").stdout):
            message = "Git maintenance base repository must be clean"
            raise GitAutomationError(message)
        base_commit = self._git_required(root, "rev-parse", "HEAD").stdout.strip()
        branch = f"{_BRANCH_PREFIX}{run_id}"
        branch_check = self._git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        if branch_check.exit_code == 0:
            message = f"Git maintenance branch already exists: {branch}"
            raise GitAutomationError(message)
        if branch_check.exit_code not in {0, 1}:
            message = f"cannot inspect Git maintenance branch: {branch_check.stderr.strip()}"
            raise GitAutomationError(message)
        path = (root / ".appmonitor" / "worktrees" / run_id).resolve()
        worktree_root = (root / ".appmonitor" / "worktrees").resolve()
        if not path.is_relative_to(worktree_root) or path.exists():
            message = f"managed worktree path is unavailable: {path}"
            raise GitAutomationError(message)
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self._git(root, "worktree", "add", "--detach", str(path), base_commit)
        if result.exit_code != 0:
            message = f"cannot create Git worktree: {result.stderr.strip()}"
            raise GitAutomationError(message)
        return PreparedWorktree(root, path, branch, base_commit)

    def commit(
        self,
        worktree: PreparedWorktree,
        *,
        allowed_paths: tuple[str, ...],
        message: str,
    ) -> GitCommitResult:
        """Create a branch and commit exactly the changed authorized paths."""
        normalized_allowed = _normalized_paths(allowed_paths)
        normalized_message = message.strip()
        if (
            not normalized_message
            or "\n" in normalized_message
            or len(normalized_message) > _MAX_COMMIT_MESSAGE_CHARS
        ):
            error = "Git commit message must be one non-empty line of at most 200 characters"
            raise GitAutomationError(error)
        changed_paths = self._changed_paths(worktree.path)
        if not changed_paths:
            error = "verified worktree contains no changes to commit"
            raise GitAutomationError(error)
        unexpected = set(changed_paths) - set(normalized_allowed)
        if unexpected:
            paths = ", ".join(sorted(unexpected))
            error = f"worktree changed paths outside authorized scope: {paths}"
            raise GitAutomationError(error)
        self._git_required(worktree.path, "switch", "-c", worktree.branch)
        self._git_required(worktree.path, "add", "--", *changed_paths)
        self._git_required(worktree.path, "commit", "-m", normalized_message)
        commit = self._git_required(worktree.path, "rev-parse", "HEAD").stdout.strip()
        return GitCommitResult(
            branch=worktree.branch,
            commit=commit,
            base_commit=worktree.base_commit,
            changed_paths=changed_paths,
        )

    def cleanup(self, worktree: PreparedWorktree) -> None:
        """Remove only the managed worktree while retaining any local branch."""
        worktree_root = (worktree.repository / ".appmonitor" / "worktrees").resolve()
        target = worktree.path.resolve()
        if not target.is_relative_to(worktree_root):
            message = f"refusing to remove unmanaged worktree path: {target}"
            raise GitAutomationError(message)
        result = self._git(
            worktree.repository,
            "worktree",
            "remove",
            "--force",
            str(target),
        )
        if result.exit_code != 0 and target.exists():
            message = f"cannot remove Git worktree: {result.stderr.strip()}"
            raise GitAutomationError(message)

    def _changed_paths(self, worktree: Path) -> tuple[str, ...]:
        """Return tracked and untracked worktree changes in stable order."""
        tracked = _nul_paths(
            self._git_required(worktree, "diff", "HEAD", "--name-only", "-z").stdout,
        )
        untracked = _nul_paths(
            self._git_required(
                worktree,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ).stdout,
        )
        return tuple(sorted(set(tracked) | set(untracked)))

    def _git_required(self, cwd: Path, *arguments: str) -> CommandResult:
        """Run Git and raise a sanitized infrastructure error on failure."""
        result = self._git(cwd, *arguments)
        if result.exit_code != 0:
            message = f"Git command failed ({arguments[0]}): {result.stderr.strip()}"
            raise GitAutomationError(message)
        return result

    def _git(self, cwd: Path, *arguments: str) -> CommandResult:
        """Run Git with an explicit safe-directory allowance and no shell."""
        command = (
            "git",
            "-c",
            f"safe.directory={cwd.resolve().as_posix()}",
            *arguments,
        )
        return self._runner.run(command, cwd=cwd)


class GitMaintenanceWorkflow:
    """Run regression and patching in isolation, then create one local commit."""

    def __init__(
        self,
        *,
        regression_workflow: RegressionWorkflow,
        patch_pipeline: PatchWorkflow,
        worktrees: GitWorktreeManager | None = None,
        store: SQLiteGitStore | None = None,
    ) -> None:
        """Inject deterministic maintenance stages and Git authority."""
        self._regression = regression_workflow
        self._patching = patch_pipeline
        self._worktrees = worktrees or GitWorktreeManager()
        self._store = store

    def execute(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
        commit_message: str | None = None,
    ) -> GitMaintenanceResult:
        """Execute V1 maintenance in a disposable worktree and commit on acceptance."""
        worktree = self._worktrees.prepare(run.report.repository, run_id=run.run_id)
        try:
            isolated_run = _isolated_run(run, worktree.path)
            regression = self._regression.generate(
                isolated_run,
                diagnostic,
                source_paths=source_paths,
                budget=budget,
            )
            if regression.status != "reproduces":
                return self._finish(
                    run.run_id,
                    GitMaintenanceResult(
                        status="rejected",
                        reason="generated test did not prove the incident",
                        branch=worktree.branch,
                        base_commit=worktree.base_commit,
                        commit=None,
                        changed_paths=(),
                        regression=regression,
                        patch=None,
                    ),
                )
            patch = self._patching.execute(
                isolated_run,
                diagnostic,
                regression,
                source_paths=source_paths,
                budget=budget,
            )
            if patch.status != "applied":
                return self._finish(
                    run.run_id,
                    GitMaintenanceResult(
                        status="rejected",
                        reason=patch.reason,
                        branch=worktree.branch,
                        base_commit=worktree.base_commit,
                        commit=None,
                        changed_paths=(),
                        regression=regression,
                        patch=patch,
                    ),
                )
            commit = self._worktrees.commit(
                worktree,
                allowed_paths=(*source_paths, regression.proposal.path),
                message=commit_message or f"appmonitor: bounded repair {run.run_id}",
            )
            return self._finish(
                run.run_id,
                GitMaintenanceResult(
                    status="committed",
                    reason="verified patch committed on isolated local branch",
                    branch=commit.branch,
                    base_commit=commit.base_commit,
                    commit=commit.commit,
                    changed_paths=commit.changed_paths,
                    regression=regression,
                    patch=patch,
                ),
            )
        finally:
            self._worktrees.cleanup(worktree)

    def _finish(self, run_id: str, result: GitMaintenanceResult) -> GitMaintenanceResult:
        """Persist an optional final decision and return it."""
        if self._store:
            self._store.save(run_id, result)
        return result


def _isolated_run(run: OrchestratedRun, worktree: Path) -> OrchestratedRun:
    """Project an existing run identity onto its isolated repository copy."""
    report = replace(run.report, repository=str(worktree))
    repository_facts = RepositoryInspector().inspect(worktree)
    return replace(run, report=report, repository_facts=repository_facts)


def _normalized_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Validate repository-local POSIX path scope."""
    normalized: list[str] = []
    for path in paths:
        pure = PurePosixPath(path)
        if (
            not path
            or "\\" in path
            or pure.is_absolute()
            or ".." in pure.parts
            or pure.as_posix() == "."
        ):
            message = f"Git authorized path is invalid: {path}"
            raise GitAutomationError(message)
        normalized.append(pure.as_posix())
    if not normalized or len(set(normalized)) != len(normalized):
        message = "Git authorized paths must be non-empty and unique"
        raise GitAutomationError(message)
    return tuple(normalized)


def _meaningful_status(output: str) -> tuple[str, ...]:
    """Ignore AppMonitor's own state directory when checking source cleanliness."""
    paths = tuple(
        line[3:].strip().strip('"')
        for line in output.splitlines()
        if len(line) >= _PORCELAIN_MIN_LINE_LENGTH
    )
    return tuple(
        path for path in paths if path != ".appmonitor" and not path.startswith(".appmonitor/")
    )


def _nul_paths(output: str) -> tuple[str, ...]:
    """Decode NUL-separated Git paths."""
    return tuple(path for path in output.split("\0") if path)

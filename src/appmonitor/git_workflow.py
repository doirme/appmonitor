"""Isolated Git worktrees and bounded local commits for verified maintenance."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol, cast

from appmonitor.models import RunSpec
from appmonitor.openrouter import BudgetExceededError
from appmonitor.persistence import SQLiteRunStore
from appmonitor.recovery import (
    RecoveryDecision,
    RecoveryDecisionMaker,
    RecoveryLimitError,
    RecoveryLimits,
)
from appmonitor.repository import RepositoryInspector, SubprocessCommandRunner

if TYPE_CHECKING:
    from appmonitor.agents import DiagnosticResult
    from appmonitor.openrouter import LLMBudget
    from appmonitor.orchestrator import OrchestratedRun, RunClient
    from appmonitor.patching import PatchPipelineResult
    from appmonitor.regression import RegressionTestResult
    from appmonitor.repository import CommandResult, CommandRunner

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRANCH_PREFIX = "appmonitor/"
_MAX_COMMIT_MESSAGE_CHARS = 200
_PORCELAIN_MIN_LINE_LENGTH = 4
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MaintenanceStatus = Literal["committed", "pushed", "rejected"]


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


class RemoteGitPreflight(Protocol):
    """Remote access check required before an opted-in monitored run."""

    def preflight(self, repository: Path, *, run_id: str, remote: str) -> None:
        """Verify remote branch publication without writing it."""


class RestartClient(Protocol):
    """Execute one corrected local run."""

    def execute(self, spec: RunSpec) -> OrchestratedRun:
        """Run and persist the corrected target."""


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
    remote: str | None = None
    pushed: bool = False
    restart_decision: RecoveryDecision | None = None
    restart_run_id: str | None = None
    restart_outcome: str | None = None

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
            "remote": self.remote,
            "pushed": self.pushed,
            "restart_decision": (
                self.restart_decision.to_dict() if self.restart_decision else None
            ),
            "restart_run_id": self.restart_run_id,
            "restart_outcome": self.restart_outcome,
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
    remote_name TEXT,
    pushed INTEGER NOT NULL DEFAULT 0 CHECK (pushed IN (0, 1)),
    restart_action TEXT,
    restart_run_id TEXT,
    restart_outcome TEXT,
    result_json TEXT NOT NULL
);
"""

_GIT_STORE_COLUMNS = {
    "remote_name": "TEXT",
    "pushed": "INTEGER NOT NULL DEFAULT 0 CHECK (pushed IN (0, 1))",
    "restart_action": "TEXT",
    "restart_run_id": "TEXT",
    "restart_outcome": "TEXT",
}


class SQLiteGitStore:
    """Persist one final Git maintenance decision against its source run."""

    def __init__(self, database: str | Path) -> None:
        """Initialize the Git maintenance table."""
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(_GIT_STORE_SCHEMA)
            _ensure_columns(connection, "run_git_maintenance", _GIT_STORE_COLUMNS)

    def save(self, run_id: str, result: GitMaintenanceResult) -> None:
        """Insert one immutable Git decision."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO run_git_maintenance (
                    run_id, status, reason, branch, base_commit,
                    commit_sha, changed_paths_json, remote_name, pushed,
                    restart_action, restart_run_id, restart_outcome, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.status,
                    result.reason,
                    result.branch,
                    result.base_commit,
                    result.commit,
                    json.dumps(result.changed_paths),
                    result.remote,
                    result.pushed,
                    result.restart_decision.action if result.restart_decision else None,
                    result.restart_run_id,
                    result.restart_outcome,
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


class GitRemotePublisher:
    """Preflight and publish one new dedicated branch without force."""

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """Use the no-shell subprocess boundary by default."""
        self._runner = runner or SubprocessCommandRunner()

    def preflight(self, repository: Path, *, run_id: str, remote: str) -> None:
        """Verify remote existence, authentication, branch absence, and push permission."""
        branch = _maintenance_branch(run_id)
        _validate_remote(remote)
        root = Path(repository).resolve()
        self._required(root, "rev-parse", "--show-toplevel")
        self._required(root, "remote", "get-url", remote)
        if self._remote_branch(root, remote, branch):
            message = f"remote maintenance branch already exists: {branch}"
            raise GitAutomationError(message)
        result = self._git(
            root,
            "push",
            "--dry-run",
            "--porcelain",
            remote,
            f"HEAD:refs/heads/{branch}",
        )
        if result.exit_code != 0:
            detail = result.stderr.strip() or "push permission check failed"
            message = (
                f"remote Git mode cannot start for {remote!r}: {detail}. "
                "Run with git_remote=None for local-only maintenance."
            )
            raise GitAutomationError(message)

    def publish(
        self,
        repository: Path,
        commit: GitCommitResult,
        *,
        remote: str,
    ) -> None:
        """Push exactly one new maintenance branch without force."""
        _validate_remote(remote)
        root = Path(repository).resolve()
        if self._remote_branch(root, remote, commit.branch):
            message = f"remote maintenance branch already exists: {commit.branch}"
            raise GitAutomationError(message)
        self._required(
            root,
            "push",
            "--porcelain",
            remote,
            f"refs/heads/{commit.branch}:refs/heads/{commit.branch}",
        )

    def _remote_branch(self, repository: Path, remote: str, branch: str) -> bool:
        """Return whether an exact remote branch already exists."""
        result = self._required(
            repository,
            "ls-remote",
            "--heads",
            remote,
            f"refs/heads/{branch}",
        )
        return bool(result.stdout.strip())

    def _required(self, cwd: Path, *arguments: str) -> CommandResult:
        """Run Git or raise a sanitized remote automation error."""
        result = self._git(cwd, *arguments)
        if result.exit_code != 0:
            message = f"remote Git command failed ({arguments[0]}): {result.stderr.strip()}"
            raise GitAutomationError(message)
        return result

    def _git(self, cwd: Path, *arguments: str) -> CommandResult:
        """Run a bounded Git argument vector."""
        command = (
            "git",
            "-c",
            f"safe.directory={cwd.resolve().as_posix()}",
            *arguments,
        )
        return self._runner.run(command, cwd=cwd)


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
        branch = _maintenance_branch(run_id)
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
    """Commit verified maintenance, optionally push and restart it locally."""

    def __init__(  # noqa: PLR0913 - independent infrastructure remains injectable
        self,
        *,
        regression_workflow: RegressionWorkflow,
        patch_pipeline: PatchWorkflow,
        worktrees: GitWorktreeManager | None = None,
        store: SQLiteGitStore | None = None,
        remote_git: GitRemotePublisher | None = None,
        restart_client: RestartClient | None = None,
        decision_maker: RecoveryDecisionMaker | None = None,
        restart_limits: RecoveryLimits | None = None,
    ) -> None:
        """Inject deterministic maintenance stages and Git authority."""
        self._regression = regression_workflow
        self._patching = patch_pipeline
        self._worktrees = worktrees or GitWorktreeManager()
        self._store = store
        self._remote_git = remote_git or GitRemotePublisher()
        self._restart_client = restart_client
        self._decision_maker = decision_maker
        self._restart_limits = restart_limits or RecoveryLimits()

    def execute(  # noqa: PLR0913 - explicit maintenance controls
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
        commit_message: str | None = None,
        restart_spec: RunSpec | None = None,
    ) -> GitMaintenanceResult:
        """Commit accepted maintenance, optionally publish and restart it."""
        if run.git_remote:
            self._remote_git.preflight(
                Path(run.report.repository),
                run_id=run.run_id,
                remote=run.git_remote,
            )
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
            pushed = run.git_remote is not None
            if run.git_remote:
                self._remote_git.publish(
                    Path(run.report.repository),
                    commit,
                    remote=run.git_remote,
                )
            decision, restarted = self._restart(
                run,
                diagnostic,
                patch,
                worktree.path,
                restart_spec,
                budget,
            )
            return self._finish(
                run.run_id,
                GitMaintenanceResult(
                    status="pushed" if pushed else "committed",
                    reason=(
                        "verified patch pushed on dedicated remote branch"
                        if pushed
                        else "verified patch committed on isolated local branch"
                    ),
                    branch=commit.branch,
                    base_commit=commit.base_commit,
                    commit=commit.commit,
                    changed_paths=commit.changed_paths,
                    regression=regression,
                    patch=patch,
                    remote=run.git_remote,
                    pushed=pushed,
                    restart_decision=decision,
                    restart_run_id=restarted.run_id if restarted else None,
                    restart_outcome=(restarted.report.outcome.value if restarted else None),
                ),
            )
        finally:
            self._worktrees.cleanup(worktree)

    def _finish(self, run_id: str, result: GitMaintenanceResult) -> GitMaintenanceResult:
        """Persist an optional final decision and return it."""
        if self._store:
            self._store.save(run_id, result)
        return result

    def _restart(  # noqa: PLR0913 - explicit recovery boundaries
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        patch: PatchPipelineResult,
        worktree: Path,
        restart_spec: RunSpec | None,
        budget: LLMBudget,
    ) -> tuple[RecoveryDecision | None, OrchestratedRun | None]:
        """Apply an optional decision and execute the corrected worktree."""
        if restart_spec is None:
            return None, None
        try:
            decision = (
                self._decision_maker.decide(diagnostic, patch, budget=budget)
                if self._decision_maker
                else RecoveryDecision(
                    action="restart",
                    reason="verified patch accepted for local restart",
                    confidence=1.0,
                )
            )
        except BudgetExceededError:
            decision = RecoveryDecision(
                action="stop",
                reason="LLM budget exhausted before recovery decision",
                confidence=1.0,
            )
        if decision.action == "stop":
            return decision, None
        try:
            self._restart_limits.begin_restart()
        except RecoveryLimitError as error:
            return (
                RecoveryDecision(
                    action="stop",
                    reason=str(error),
                    confidence=1.0,
                ),
                None,
            )
        client = self._restart_client or _persistent_restart_client(
            Path(run.report.repository),
        )
        return decision, client.execute(_restart_spec(restart_spec, worktree))


def _isolated_run(run: OrchestratedRun, worktree: Path) -> OrchestratedRun:
    """Project an existing run identity onto its isolated repository copy."""
    report = replace(run.report, repository=str(worktree))
    repository_facts = RepositoryInspector().inspect(worktree)
    return replace(run, report=report, repository_facts=repository_facts)


def _maintenance_branch(run_id: str) -> str:
    """Return the only branch namespace authorized for one run."""
    if not _RUN_ID.fullmatch(run_id):
        message = "run_id is not safe for a Git branch or worktree path"
        raise GitAutomationError(message)
    return f"{_BRANCH_PREFIX}{run_id}"


def _validate_remote(remote: str) -> None:
    """Reject option-like or ambiguous remote names."""
    if not _REMOTE_NAME.fullmatch(remote):
        message = "Git remote name is not safe"
        raise GitAutomationError(message)


def _persistent_restart_client(repository: Path) -> RunClient:
    """Create a restart client that persists outside disposable worktrees."""
    from appmonitor.orchestrator import RunClient  # noqa: PLC0415 - avoids module cycle

    database = repository / ".appmonitor" / "runs.sqlite3"
    return RunClient(store=SQLiteRunStore(database))


def _restart_spec(spec: RunSpec, worktree: Path) -> RunSpec:
    """Project an authorized command onto the corrected worktree."""
    goal_file = spec.goal_file
    if goal_file is not None and goal_file.is_relative_to(spec.repository):
        goal_file = worktree / goal_file.relative_to(spec.repository)
    return RunSpec(
        repository=worktree,
        command=spec.command,
        timeout_seconds=spec.timeout_seconds,
        base_branch=spec.base_branch,
        goal_file=goal_file,
        environment=spec.environment,
        sync_environment=spec.sync_environment,
        analyze_repository=spec.analyze_repository,
        git_remote=None,
    )


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


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    """Add known nullable/defaulted columns to an existing AppMonitor table."""
    existing = {
        cast("str", row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, declaration in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

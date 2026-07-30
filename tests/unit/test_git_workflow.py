"""Tests for isolated local Git maintenance and bounded commits."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from appmonitor import LLMBudget, RunClient, RunSpec, SQLiteRunStore
from appmonitor.git_workflow import (
    GitAutomationError,
    GitMaintenanceWorkflow,
    GitRemotePublisher,
    GitWorktreeManager,
    SQLiteGitStore,
)
from appmonitor.patching import (
    PatchFilePlan,
    PatchPipelineResult,
    PatchPlan,
    PatchValidation,
)
from appmonitor.recovery import RecoveryDecision, RecoveryLimits
from appmonitor.regression import RegressionTestResult
from appmonitor.regression import TestProposal as RegressionProposal

if TYPE_CHECKING:
    from appmonitor.agents import DiagnosticResult
    from appmonitor.orchestrator import OrchestratedRun

_GIT_SHA1_LENGTH = 40


class FakeRegressionWorkflow:
    """Create one proven test inside the worktree supplied by the Git workflow."""

    def generate(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
    ) -> RegressionTestResult:
        """Write a deterministic test proposal and mark it reproducing."""
        del diagnostic, source_paths, budget
        target = Path(run.report.repository) / "tests" / "test_generated.py"
        target.parent.mkdir()
        content = "def test_generated():\n    assert True\n"
        target.write_text(content, encoding="utf-8")
        return RegressionTestResult(
            proposal=RegressionProposal(
                path="tests/test_generated.py",
                content=content,
                target_behavior="repair remains covered",
                rationale="fixture",
            ),
            status="reproduces",
            pytest_exit_code=1,
            stdout="failed as expected",
            stderr="",
        )


class FakePatchPipeline:
    """Apply one already-validated source change inside the worktree."""

    def execute(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        regression: RegressionTestResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
    ) -> PatchPipelineResult:
        """Modify the authorized source and return an accepted decision."""
        del diagnostic, regression, budget
        source = Path(run.report.repository) / source_paths[0]
        source.write_text("def value():\n    return 2\n", encoding="utf-8")
        plan = PatchPlan(
            summary="repair value",
            files=(PatchFilePlan(source_paths[0], "fix behavior"),),
            risk="low",
            acceptance_criteria=("tests pass",),
        )
        return PatchPipelineResult(
            status="applied",
            reason="validated and independently approved",
            plan=plan,
            patch_sha256="a" * 64,
            diff="bounded diff",
            validation=PatchValidation(()),
            review=None,
        )


class NonReproducingWorkflow(FakeRegressionWorkflow):
    """Return a generated test that does not prove the incident."""

    def generate(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
    ) -> RegressionTestResult:
        """Reuse the fixture proposal with a rejected status."""
        result = super().generate(
            run,
            diagnostic,
            source_paths=source_paths,
            budget=budget,
        )
        return replace(result, status="does_not_reproduce", pytest_exit_code=0)


class RejectedPatchPipeline(FakePatchPipeline):
    """Return a deterministic patch rejection."""

    def execute(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        regression: RegressionTestResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
    ) -> PatchPipelineResult:
        """Apply fixture behavior but expose a rejected final decision."""
        result = super().execute(
            run,
            diagnostic,
            regression,
            source_paths=source_paths,
            budget=budget,
        )
        return replace(result, status="rejected", reason="validation failed")


def test_worktree_commit_contains_only_authorized_paths(tmp_path: Path) -> None:
    """A verified worktree becomes a dedicated local branch and atomic commit."""
    repository = _git_repository(tmp_path)
    manager = GitWorktreeManager()
    worktree = manager.prepare(repository, run_id="run-123")
    try:
        source = worktree.path / "src" / "example.py"
        source.write_text("def value():\n    return 2\n", encoding="utf-8")

        result = manager.commit(
            worktree,
            allowed_paths=("src/example.py",),
            message="appmonitor: repair example",
        )
    finally:
        manager.cleanup(worktree)

    assert result.branch == "appmonitor/run-123"
    assert len(result.commit) == _GIT_SHA1_LENGTH
    assert result.changed_paths == ("src/example.py",)
    assert not worktree.path.exists()
    assert _git(repository, "show", f"{result.commit}:src/example.py").endswith("return 2")
    assert _git(repository, "branch", "--list", result.branch) == result.branch


def test_worktree_rejects_unexpected_changed_path(tmp_path: Path) -> None:
    """A model or tool cannot smuggle an additional file into the commit."""
    repository = _git_repository(tmp_path)
    manager = GitWorktreeManager()
    worktree = manager.prepare(repository, run_id="run-scope")
    try:
        (worktree.path / "unexpected.txt").write_text("scope creep", encoding="utf-8")

        with pytest.raises(GitAutomationError, match="outside authorized scope"):
            manager.commit(
                worktree,
                allowed_paths=("src/example.py",),
                message="appmonitor: reject scope creep",
            )
    finally:
        manager.cleanup(worktree)


def test_worktree_requires_clean_base_repository(tmp_path: Path) -> None:
    """Maintenance never starts from an ambiguous dirty source checkout."""
    repository = _git_repository(tmp_path)
    (repository / "src" / "example.py").write_text(
        "def value():\n    return 99\n",
        encoding="utf-8",
    )

    with pytest.raises(GitAutomationError, match="must be clean"):
        GitWorktreeManager().prepare(repository, run_id="dirty")


@pytest.mark.parametrize("run_id", ["", "../escape", "name with spaces"])
def test_worktree_rejects_unsafe_run_identifier(tmp_path: Path, run_id: str) -> None:
    """Run identity cannot escape the managed branch and directory namespace."""
    repository = _git_repository(tmp_path)

    with pytest.raises(GitAutomationError, match="run_id"):
        GitWorktreeManager().prepare(repository, run_id=run_id)


def test_worktree_rejects_empty_commit_and_invalid_scope(tmp_path: Path) -> None:
    """A worktree cannot create empty commits or accept traversal scope."""
    repository = _git_repository(tmp_path)
    manager = GitWorktreeManager()
    worktree = manager.prepare(repository, run_id="run-empty")
    try:
        with pytest.raises(GitAutomationError, match="invalid"):
            manager.commit(
                worktree,
                allowed_paths=("../outside.py",),
                message="appmonitor: invalid scope",
            )
        with pytest.raises(GitAutomationError, match="no changes"):
            manager.commit(
                worktree,
                allowed_paths=("src/example.py",),
                message="appmonitor: empty",
            )
    finally:
        manager.cleanup(worktree)


def test_maintenance_workflow_runs_and_commits_inside_worktree(tmp_path: Path) -> None:
    """Regression and patch stages receive the isolated path, not the source checkout."""
    repository = _git_repository(tmp_path)
    run = RunClient().execute(
        RunSpec(
            repository=repository,
            command=(sys.executable, "-c", "print('observed')"),
        ),
    )
    workflow = GitMaintenanceWorkflow(
        regression_workflow=FakeRegressionWorkflow(),
        patch_pipeline=FakePatchPipeline(),
        store=SQLiteGitStore(repository / ".appmonitor" / "runs.sqlite3"),
    )

    result = workflow.execute(
        run,
        cast("DiagnosticResult", object()),
        source_paths=("src/example.py",),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    )

    assert result.status == "committed"
    assert result.commit is not None
    assert result.changed_paths == ("src/example.py", "tests/test_generated.py")
    assert (repository / "src" / "example.py").read_text(encoding="utf-8").endswith("return 1\n")
    assert _git(repository, "show", f"{result.commit}:src/example.py").endswith("return 2")
    assert _git(repository, "show", f"{result.commit}:tests/test_generated.py")
    stored = SQLiteGitStore(repository / ".appmonitor" / "runs.sqlite3").load(run.run_id)
    assert stored["status"] == "committed"
    assert stored["commit"] == result.commit


@pytest.mark.parametrize(
    ("regression_workflow", "patch_pipeline", "expected_reason"),
    [
        (
            NonReproducingWorkflow(),
            FakePatchPipeline(),
            "generated test did not prove the incident",
        ),
        (
            FakeRegressionWorkflow(),
            RejectedPatchPipeline(),
            "validation failed",
        ),
    ],
)
def test_maintenance_rejection_creates_no_branch_or_commit(
    tmp_path: Path,
    regression_workflow: FakeRegressionWorkflow,
    patch_pipeline: FakePatchPipeline,
    expected_reason: str,
) -> None:
    """Rejected maintenance is discarded with its detached worktree."""
    repository = _git_repository(tmp_path)
    run = RunClient().execute(
        RunSpec(repository=repository, command=(sys.executable, "-c", "pass")),
    )
    workflow = GitMaintenanceWorkflow(
        regression_workflow=regression_workflow,
        patch_pipeline=patch_pipeline,
    )

    result = workflow.execute(
        run,
        cast("DiagnosticResult", object()),
        source_paths=("src/example.py",),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    )

    assert result.status == "rejected"
    assert result.reason == expected_reason
    assert result.commit is None
    assert _git(repository, "branch", "--list", result.branch) == ""


def test_remote_preflight_and_publication_push_only_dedicated_branch(tmp_path: Path) -> None:
    """Opt-in remote mode verifies access then publishes the accepted branch."""
    repository = _git_repository(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "origin", "main")
    base_commit = _git(repository, "rev-parse", "main")
    run = RunClient().execute(
        RunSpec(
            repository=repository,
            command=(sys.executable, "-c", "import sys; sys.exit(1)"),
            git_remote="origin",
        ),
    )
    workflow = GitMaintenanceWorkflow(
        regression_workflow=FakeRegressionWorkflow(),
        patch_pipeline=FakePatchPipeline(),
        store=SQLiteGitStore(repository / ".appmonitor" / "runs.sqlite3"),
    )

    result = workflow.execute(
        run,
        cast("DiagnosticResult", object()),
        source_paths=("src/example.py",),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    )

    assert result.status == "pushed"
    assert result.remote == "origin"
    assert result.pushed is True
    assert _git(repository, "ls-remote", "--heads", "origin", result.branch)
    assert _git(repository, "rev-parse", "main") == base_commit


def test_remote_preflight_rejects_existing_maintenance_branch(tmp_path: Path) -> None:
    """A remote branch is never overwritten or force-pushed."""
    repository = _git_repository(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "origin", "main:appmonitor/collision")

    with pytest.raises(GitAutomationError, match="already exists"):
        GitRemotePublisher().preflight(
            repository,
            run_id="collision",
            remote="origin",
        )


def test_git_store_adds_phase_9b_columns_to_existing_database(tmp_path: Path) -> None:
    """Opening a V1 database applies only additive Git audit columns."""
    database = tmp_path / "runs.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE run_git_maintenance (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                branch TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                commit_sha TEXT,
                changed_paths_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """,
        )

    SQLiteGitStore(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(run_git_maintenance)")}
    assert {
        "remote_name",
        "pushed",
        "restart_action",
        "restart_run_id",
        "restart_outcome",
    } <= columns


def test_accepted_patch_restarts_locally_from_corrected_worktree(tmp_path: Path) -> None:
    """The restart command observes committed repaired code before worktree cleanup."""
    repository = _git_repository(tmp_path)
    database = repository / ".appmonitor" / "runs.sqlite3"
    run = RunClient(store=SQLiteRunStore(database)).execute(
        RunSpec(
            repository=repository,
            command=(sys.executable, "-c", "import sys; sys.exit(1)"),
        ),
    )
    workflow = GitMaintenanceWorkflow(
        regression_workflow=FakeRegressionWorkflow(),
        patch_pipeline=FakePatchPipeline(),
        store=SQLiteGitStore(database),
        restart_limits=RecoveryLimits(max_restarts=3, max_duration_seconds=60),
    )

    result = workflow.execute(
        run,
        cast("DiagnosticResult", object()),
        source_paths=("src/example.py",),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
        restart_spec=RunSpec(
            repository=repository,
            command=(
                sys.executable,
                "-c",
                "from src.example import value; raise SystemExit(value() != 2)",
            ),
        ),
    )

    assert result.restart_decision is not None
    assert result.restart_decision.action == "restart"
    assert result.restart_run_id is not None
    assert result.restart_outcome == "succeeded"
    assert SQLiteRunStore(database).load(result.restart_run_id)["outcome"] == "succeeded"


class StopDecisionMaker:
    """Return an explicit no-restart decision."""

    def decide(
        self,
        diagnostic: DiagnosticResult,
        patch: PatchPipelineResult,
        *,
        budget: LLMBudget,
    ) -> RecoveryDecision:
        """Stop without consuming the supplied fixtures."""
        del diagnostic, patch, budget
        return RecoveryDecision(
            action="stop",
            reason="critical defect remains unsafe",
            confidence=1.0,
            call_id="decision-1",
            model="reviewer/model",
        )


def test_llm_stop_decision_prevents_local_restart(tmp_path: Path) -> None:
    """A structured stop recommendation leaves the repaired branch inactive."""
    repository = _git_repository(tmp_path)
    run = RunClient().execute(
        RunSpec(repository=repository, command=(sys.executable, "-c", "pass")),
    )
    workflow = GitMaintenanceWorkflow(
        regression_workflow=FakeRegressionWorkflow(),
        patch_pipeline=FakePatchPipeline(),
        decision_maker=StopDecisionMaker(),
    )

    result = workflow.execute(
        run,
        cast("DiagnosticResult", object()),
        source_paths=("src/example.py",),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
        restart_spec=RunSpec(repository=repository, command=(sys.executable, "-c", "pass")),
    )

    assert result.restart_decision is not None
    assert result.restart_decision.action == "stop"
    assert result.restart_run_id is None


def _git_repository(tmp_path: Path) -> Path:
    """Create one clean repository with local commit identity."""
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / ".gitignore").write_text(".appmonitor/\n", encoding="utf-8")
    (repository / "src" / "example.py").write_text(
        "def value():\n    return 1\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "AppMonitor Tests")
    _git(repository, "config", "user.email", "appmonitor@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    return repository


def _git(repository: Path, *arguments: str) -> str:
    """Run Git for integration assertions."""
    completed = subprocess.run(  # noqa: S603 - fixed Git test boundary
        ("git", *arguments),  # noqa: S607 - Git is the explicit test executable
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()

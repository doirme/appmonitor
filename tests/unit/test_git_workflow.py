"""Tests for isolated local Git maintenance and bounded commits."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from appmonitor import LLMBudget, RunClient, RunSpec
from appmonitor.git_workflow import (
    GitAutomationError,
    GitMaintenanceWorkflow,
    GitWorktreeManager,
    SQLiteGitStore,
)
from appmonitor.patching import (
    PatchFilePlan,
    PatchPipelineResult,
    PatchPlan,
    PatchValidation,
)
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

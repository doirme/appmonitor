"""Tests for bounded transactional patching."""

import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from appmonitor import LLMBudget, RunClient, RunSpec
from appmonitor.agents import DiagnosticResult, RunAssessment
from appmonitor.openrouter import ChatMessage, LLMUsage, StructuredCompletion
from appmonitor.patching import (
    AtomicPatchApplier,
    FileReplacement,
    PatchFilePlan,
    PatchImplementerAgent,
    PatchPipeline,
    PatchPlan,
    PatchPlannerAgent,
    PatchPolicy,
    PatchPolicyError,
    PatchProposal,
    PatchReviewerAgent,
    SQLitePatchStore,
)
from appmonitor.regression import RegressionTestResult
from appmonitor.regression import TestProposal as GeneratedTestProposal
from appmonitor.repository import CommandResult

_VALIDATION_CHECK_COUNT = 5


class FakeStructuredClient:
    """Return ordered planner, implementer, and reviewer outputs."""

    def __init__(self, outputs: list[dict[str, object]]) -> None:
        """Store outputs and task names."""
        self.outputs = outputs
        self.tasks: list[str] = []
        self.schemas: list[dict[str, object]] = []

    def complete_structured(  # noqa: PLR0913 - implements the production protocol
        self,
        *,
        task: str,
        messages: tuple[ChatMessage, ...],
        schema_name: str,
        schema: dict[str, object],
        budget: LLMBudget,
        min_context_tokens: int = 8_000,
        max_output_tokens: int = 1_000,
        max_attempts: int = 1,
    ) -> StructuredCompletion:
        """Consume one budgeted fixed output."""
        del messages, schema_name, min_context_tokens, max_output_tokens, max_attempts
        budget.begin_call(0)
        budget.finish_call(0, 0)
        self.tasks.append(task)
        self.schemas.append(schema)
        return StructuredCompletion(
            call_id=f"call-{len(self.tasks)}",
            model="fake/model",
            data=self.outputs.pop(0),
            usage=LLMUsage(10, 10, 0),
            latency_seconds=0.01,
        )


class SequenceRunner:
    """Return configured validation exit codes."""

    def __init__(self, exit_codes: list[int]) -> None:
        """Store exit codes and commands."""
        self.exit_codes = exit_codes
        self.commands: list[tuple[str, ...]] = []

    def run(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Return the next command result."""
        del cwd
        self.commands.append(command)
        return CommandResult(self.exit_codes.pop(0), "validation", "")


def _diagnostic() -> DiagnosticResult:
    return DiagnosticResult(
        assessment=RunAssessment(
            summary="calculator.add subtracts instead of adding",
            goal_alignment="violated",
            findings=(),
            needs_investigation=True,
            confidence=1.0,
        ),
        assessment_call_id="critic",
    )


def _regression() -> RegressionTestResult:
    return RegressionTestResult(
        proposal=GeneratedTestProposal(
            path="tests/test_calculator.py",
            content="def test_add():\n    assert add(2, 3) == 5\n",
            target_behavior="Addition returns the sum.",
            rationale="Reproduces subtraction bug.",
        ),
        status="reproduces",
        pytest_exit_code=1,
        stdout="failed",
        stderr="",
    )


def _plan_output() -> dict[str, object]:
    return {
        "summary": "Correct the arithmetic operator.",
        "files": [{"path": "calculator.py", "rationale": "Contains the faulty operation."}],
        "risk": "low",
        "acceptance_criteria": ["The generated regression test passes."],
    }


def _proposal_output(original: str, replacement: str) -> dict[str, object]:
    return {
        "summary": "Use addition.",
        "replacements": [
            {
                "path": "calculator.py",
                "original_sha256": sha256(original.encode()).hexdigest(),
                "content": replacement,
                "rationale": "Replace subtraction with addition.",
            },
        ],
    }


def _review_output(*, approved: bool = True) -> dict[str, object]:
    return {
        "verdict": "approve" if approved else "reject",
        "summary": "The patch is minimal." if approved else "The patch is not acceptable.",
        "findings": [] if approved else ["The behavior remains incorrect."],
        "confidence": 0.95,
    }


def test_policy_rejects_unplanned_hash_mismatch_and_test_changes(tmp_path: Path) -> None:
    """Only planned existing source files at their exact version are eligible."""
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    plan = PatchPlan("fix", (PatchFilePlan("calculator.py", "bug"),), "low", ("pass",))

    with pytest.raises(PatchPolicyError, match="not declared"):
        PatchPolicy().authorize(
            tmp_path,
            plan,
            PatchProposal(
                "bad",
                (FileReplacement("other.py", "digest", "x = 1\n", "bad"),),
            ),
            allowed_source_paths=("calculator.py",),
        )
    with pytest.raises(PatchPolicyError, match="hash"):
        PatchPolicy().authorize(
            tmp_path,
            plan,
            PatchProposal(
                "bad",
                (FileReplacement("calculator.py", "wrong", "x = 1\n", "bad"),),
            ),
            allowed_source_paths=("calculator.py",),
        )
    with pytest.raises(PatchPolicyError, match="source"):
        PatchPolicy().authorize(
            tmp_path,
            PatchPlan("bad", (PatchFilePlan("tests/test_x.py", "bad"),), "low", ("pass",)),
            PatchProposal(
                "bad",
                (FileReplacement("tests/test_x.py", "digest", "x = 1\n", "bad"),),
            ),
            allowed_source_paths=("tests/test_x.py",),
        )


def test_policy_rejects_duplicate_empty_and_oversized_proposals(tmp_path: Path) -> None:
    """Cardinality, uniqueness, and changed-line limits are enforced locally."""
    original = "value = 1\n"
    source = tmp_path / "module.py"
    source.write_bytes(original.encode())
    file_plan = PatchFilePlan("module.py", "change")
    replacement = FileReplacement(
        "module.py",
        sha256(original.encode()).hexdigest(),
        "value = 2\n",
        "change",
    )
    policy = PatchPolicy(max_changed_lines=1)

    with pytest.raises(PatchPolicyError, match="duplicate"):
        policy.authorize(
            tmp_path,
            PatchPlan("bad", (file_plan, file_plan), "low", ("pass",)),
            PatchProposal("change", (replacement,)),
            allowed_source_paths=("module.py",),
        )
    with pytest.raises(PatchPolicyError, match="at least one"):
        policy.authorize(
            tmp_path,
            PatchPlan("bad", (file_plan,), "low", ("pass",)),
            PatchProposal("change", ()),
            allowed_source_paths=("module.py",),
        )
    with pytest.raises(PatchPolicyError, match="limit"):
        policy.authorize(
            tmp_path,
            PatchPlan("bad", (file_plan,), "low", ("pass",)),
            PatchProposal("change", (replacement,)),
            allowed_source_paths=("module.py",),
        )


def test_policy_rejects_unchanged_and_invalid_python(tmp_path: Path) -> None:
    """A replacement must change bytes and remain parseable Python."""
    original = "value = 1\n"
    source = tmp_path / "module.py"
    source.write_bytes(original.encode())
    plan = PatchPlan("fix", (PatchFilePlan("module.py", "change"),), "low", ("pass",))
    digest = sha256(original.encode()).hexdigest()

    with pytest.raises(PatchPolicyError, match="does not change"):
        PatchPolicy().authorize(
            tmp_path,
            plan,
            PatchProposal("same", (FileReplacement("module.py", digest, original, "same"),)),
            allowed_source_paths=("module.py",),
        )
    with pytest.raises(PatchPolicyError, match="invalid Python"):
        PatchPolicy().authorize(
            tmp_path,
            plan,
            PatchProposal(
                "syntax",
                (FileReplacement("module.py", digest, "def broken(:\n", "syntax"),),
            ),
            allowed_source_paths=("module.py",),
        )


def test_atomic_applier_rolls_back_without_commit(tmp_path: Path) -> None:
    """Leaving a patch transaction uncommitted restores exact original bytes."""
    source = tmp_path / "calculator.py"
    original = "def add(a, b):\n    return a - b\n"
    replacement = "def add(a, b):\n    return a + b\n"
    source.write_bytes(original.encode())
    patch = PatchPolicy().authorize(
        tmp_path,
        PatchPlan("fix", (PatchFilePlan("calculator.py", "bug"),), "low", ("pass",)),
        PatchProposal(
            "fix",
            (
                FileReplacement(
                    "calculator.py",
                    sha256(original.encode()).hexdigest(),
                    replacement,
                    "operator",
                ),
            ),
        ),
        allowed_source_paths=("calculator.py",),
    )

    with AtomicPatchApplier().apply(patch):
        assert source.read_text(encoding="utf-8") == replacement

    assert source.read_text(encoding="utf-8") == original


def test_pipeline_applies_only_after_validation_and_review(tmp_path: Path) -> None:
    """A verified and approved patch remains in the working directory."""
    original = "def add(a: int, b: int) -> int:\n    return a - b\n"
    replacement = "def add(a: int, b: int) -> int:\n    return a + b\n"
    (tmp_path / "calculator.py").write_bytes(original.encode())
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calculator.py").write_text(
        "def test_add():\n    assert True\n",
        encoding="utf-8",
    )
    run = RunClient().execute(
        RunSpec(repository=tmp_path, command=("python", "-c", "import sys; sys.exit(1)")),
    )
    client = FakeStructuredClient(
        [_plan_output(), _proposal_output(original, replacement), _review_output()],
    )
    runner = SequenceRunner([0, 0, 0, 0, 0])
    pipeline = PatchPipeline(
        planner=PatchPlannerAgent(client),
        implementer=PatchImplementerAgent(client),
        reviewer=PatchReviewerAgent(client),
        runner=runner,
        store=SQLitePatchStore(tmp_path / ".appmonitor" / "runs.sqlite3"),
    )

    result = pipeline.execute(
        run,
        _diagnostic(),
        _regression(),
        source_paths=("calculator.py",),
        budget=LLMBudget(max_calls=3, max_cost_usd=0.01),
    )

    assert result.status == "applied"
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == replacement
    assert client.tasks == ["patch_planner", "patch_implementer", "patch_reviewer"]
    assert '"enum": ["calculator.py"]' in json.dumps(client.schemas[0], sort_keys=True)
    assert '"enum": ["calculator.py"]' in json.dumps(client.schemas[1], sort_keys=True)
    assert len(runner.commands) == _VALIDATION_CHECK_COUNT
    assert runner.commands[-1][4:7] == ("compileall", "-q", "-x")
    assert result.review is not None
    assert result.to_dict()["review"] == result.review.to_dict()
    with sqlite3.connect(tmp_path / ".appmonitor" / "runs.sqlite3") as connection:
        row = connection.execute(
            "SELECT status, reason, patch_sha256, review_json FROM run_patches WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row[:3] == (result.status, result.reason, result.patch_sha256)
    assert json.loads(row[3])["verdict"] == "approve"


@pytest.mark.parametrize(
    ("exit_codes", "review_verdict", "expected_reason", "expected_tasks"),
    [
        ([1], "approve", "validation failed", ["patch_planner", "patch_implementer"]),
        (
            [0, 0, 0, 0, 0],
            "reject",
            "review rejected",
            [
                "patch_planner",
                "patch_implementer",
                "patch_reviewer",
            ],
        ),
    ],
)
def test_pipeline_rolls_back_rejected_patch(
    tmp_path: Path,
    exit_codes: list[int],
    review_verdict: str,
    expected_reason: str,
    expected_tasks: list[str],
) -> None:
    """Validation or reviewer rejection restores the original source."""
    original = "def add(a: int, b: int) -> int:\n    return a - b\n"
    replacement = "def add(a: int, b: int) -> int:\n    return a + b\n"
    (tmp_path / "calculator.py").write_bytes(original.encode())
    run = RunClient().execute(
        RunSpec(repository=tmp_path, command=("python", "-c", "import sys; sys.exit(1)")),
    )
    outputs = [_plan_output(), _proposal_output(original, replacement)]
    if len(exit_codes) > 1:
        outputs.append(_review_output(approved=review_verdict == "approve"))
    client = FakeStructuredClient(outputs)
    pipeline = PatchPipeline(
        planner=PatchPlannerAgent(client),
        implementer=PatchImplementerAgent(client),
        reviewer=PatchReviewerAgent(client),
        runner=SequenceRunner(exit_codes),
    )

    result = pipeline.execute(
        run,
        _diagnostic(),
        _regression(),
        source_paths=("calculator.py",),
        budget=LLMBudget(max_calls=3, max_cost_usd=0.01),
    )

    assert result.status == "rejected"
    assert result.reason == expected_reason
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == original
    assert client.tasks == expected_tasks


def test_pipeline_requires_proven_regression_and_source_scope(tmp_path: Path) -> None:
    """Mutation cannot begin without both deterministic prerequisites."""
    run = RunClient().execute(
        RunSpec(repository=tmp_path, command=("python", "-c", "import sys; sys.exit(1)")),
    )
    client = FakeStructuredClient([])
    pipeline = PatchPipeline(
        planner=PatchPlannerAgent(client),
        implementer=PatchImplementerAgent(client),
        reviewer=PatchReviewerAgent(client),
    )

    with pytest.raises(PatchPolicyError, match="proven"):
        pipeline.execute(
            run,
            _diagnostic(),
            replace(_regression(), status="does_not_reproduce"),
            source_paths=("module.py",),
            budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
        )
    with pytest.raises(PatchPolicyError, match="non-empty"):
        pipeline.execute(
            run,
            _diagnostic(),
            _regression(),
            source_paths=(),
            budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
        )

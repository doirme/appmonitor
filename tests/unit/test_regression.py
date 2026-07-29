"""Tests for bounded regression-test generation."""

import sqlite3
import sys
from pathlib import Path

import pytest

from appmonitor import LLMBudget, RunClient, RunSpec
from appmonitor.agents import DiagnosticResult, RunAssessment
from appmonitor.openrouter import ChatMessage, LLMUsage, StructuredCompletion
from appmonitor.regression import (
    BoundedPytestRunner,
    GeneratedTestPolicy,
    RegressionTestGenerator,
    RegressionTestWorkflow,
    SourceContextError,
    SQLiteRegressionStore,
    collect_source_context,
)
from appmonitor.regression import (
    TestPolicyError as GeneratedTestPolicyError,
)
from appmonitor.regression import (
    TestProposal as GeneratedTestProposal,
)
from appmonitor.repository import CommandResult

_COMMAND_NOT_FOUND_EXIT_CODE = 127


class FakeStructuredClient:
    """Return one fixed test proposal."""

    def __init__(self, output: dict[str, object]) -> None:
        """Store output and captured messages."""
        self.output = output
        self.messages: tuple[ChatMessage, ...] = ()

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
        """Capture the prompt and return schema-shaped data."""
        del task, schema_name, schema, min_context_tokens, max_output_tokens, max_attempts
        budget.begin_call(0)
        budget.finish_call(0, 0)
        self.messages = messages
        return StructuredCompletion(
            call_id="test-call",
            model="fake/model",
            data=self.output,
            usage=LLMUsage(10, 10, 0),
            latency_seconds=0.01,
        )


class FixedRunner:
    """Return one fixed pytest command result."""

    def __init__(self, exit_code: int) -> None:
        """Store the desired pytest exit status."""
        self.exit_code = exit_code
        self.commands: list[tuple[str, ...]] = []

    def run(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Record the fixed command and return its result."""
        del cwd
        self.commands.append(command)
        return CommandResult(self.exit_code, "pytest output", "")


def _proposal(
    content: str = "def test_regression():\n    assert 1 == 2\n",
) -> GeneratedTestProposal:
    return GeneratedTestProposal(
        path="tests/test_generated_regression.py",
        content=content,
        target_behavior="The incorrect result is rejected.",
        rationale="Reproduces the observed behavior.",
    )


@pytest.mark.parametrize(
    "proposal",
    [
        GeneratedTestProposal("../outside.py", "def test_x():\n    assert False\n", "x", "x"),
        GeneratedTestProposal(
            "tests/test_network.py",
            "import socket\n\ndef test_x():\n    assert socket\n",
            "x",
            "x",
        ),
        GeneratedTestProposal(
            "tests/test_write.py",
            "from pathlib import Path\n\ndef test_x():\n"
            "    Path('x').write_text('bad', encoding='utf-8')\n",
            "x",
            "x",
        ),
        GeneratedTestProposal("tests/not_python.txt", "test", "x", "x"),
    ],
)
def test_policy_rejects_unsafe_generated_tests(
    tmp_path: Path,
    proposal: GeneratedTestProposal,
) -> None:
    """Unsafe paths, capabilities, and file types are rejected before writing."""
    with pytest.raises(GeneratedTestPolicyError):
        GeneratedTestPolicy().validate(tmp_path, proposal)


def test_generator_uses_only_explicit_source_context() -> None:
    """The model receives supplied source snippets and structured diagnostics."""
    client = FakeStructuredClient(
        {
            "path": "tests/test_bug.py",
            "content": "def test_bug():\n    assert False\n",
            "target_behavior": "Bug is reproduced.",
            "rationale": "Covers the incident.",
        },
    )
    diagnostic = DiagnosticResult(
        assessment=RunAssessment(
            summary="failed",
            goal_alignment="violated",
            findings=(),
            needs_investigation=True,
            confidence=1.0,
        ),
        assessment_call_id="critic-call",
    )

    proposal = RegressionTestGenerator(client).propose(
        diagnostic,
        source_context={"src/example.py": "def broken():\n    return 1\n"},
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    )

    assert proposal.path == "tests/test_bug.py"
    prompt = client.messages[-1].content
    assert "src/example.py" in prompt
    assert "def broken" in prompt


def test_workflow_keeps_only_a_reproducing_test(tmp_path: Path) -> None:
    """Pytest exit code 1 proves reproduction and keeps the generated file."""
    run = RunClient().execute(
        RunSpec(repository=tmp_path, command=[sys.executable, "-c", "import sys; sys.exit(1)"]),
    )
    diagnostic = DiagnosticResult(
        assessment=RunAssessment(
            summary="failed",
            goal_alignment="violated",
            findings=(),
            needs_investigation=True,
            confidence=1.0,
        ),
        assessment_call_id="critic-call",
    )
    runner = FixedRunner(exit_code=1)
    workflow = RegressionTestWorkflow(
        generator=RegressionTestGenerator(
            FakeStructuredClient(
                {
                    "path": _proposal().path,
                    "content": _proposal().content,
                    "target_behavior": _proposal().target_behavior,
                    "rationale": _proposal().rationale,
                },
            ),
        ),
        runner=runner,
    )

    result = workflow.generate(
        run,
        diagnostic,
        source_paths=(),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    )

    assert result.status == "reproduces"
    assert (tmp_path / result.proposal.path).is_file()
    assert runner.commands == [
        (
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "--rootdir=.",
            "tests/test_generated_regression.py",
            "-q",
        ),
    ]


def test_workflow_removes_non_reproducing_test(tmp_path: Path) -> None:
    """A passing generated test is rejected and removed."""
    run = RunClient().execute(
        RunSpec(repository=tmp_path, command=[sys.executable, "-c", "import sys; sys.exit(1)"]),
    )
    diagnostic = DiagnosticResult(
        assessment=RunAssessment(
            summary="failed",
            goal_alignment="violated",
            findings=(),
            needs_investigation=True,
            confidence=1.0,
        ),
        assessment_call_id="critic-call",
    )
    workflow = RegressionTestWorkflow(
        generator=RegressionTestGenerator(
            FakeStructuredClient(
                {
                    "path": _proposal().path,
                    "content": _proposal().content,
                    "target_behavior": _proposal().target_behavior,
                    "rationale": _proposal().rationale,
                },
            ),
        ),
        runner=FixedRunner(exit_code=0),
    )

    result = workflow.generate(
        run,
        diagnostic,
        source_paths=(),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    )

    assert result.status == "does_not_reproduce"
    assert not (tmp_path / result.proposal.path).exists()


def test_policy_rejects_existing_invalid_or_assertion_free_test(tmp_path: Path) -> None:
    """Existing files, syntax errors, and tests without checks cannot be authorized."""
    existing = tmp_path / "tests" / "test_existing.py"
    existing.parent.mkdir()
    existing.write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    policy = GeneratedTestPolicy()

    with pytest.raises(GeneratedTestPolicyError, match="already exists"):
        policy.validate(
            tmp_path,
            GeneratedTestProposal(existing.relative_to(tmp_path).as_posix(), "x", "x", "x"),
        )
    with pytest.raises(GeneratedTestPolicyError, match="invalid Python"):
        policy.validate(
            tmp_path,
            GeneratedTestProposal("tests/test_syntax.py", "def broken(:\n", "x", "x"),
        )
    with pytest.raises(GeneratedTestPolicyError, match="must define"):
        policy.validate(
            tmp_path,
            GeneratedTestProposal(
                "tests/test_empty.py",
                "def test_empty():\n    pass\n",
                "x",
                "x",
            ),
        )


def test_policy_rejects_dynamic_and_dunder_access(tmp_path: Path) -> None:
    """Dynamic execution and reflective dunder access are denied."""
    with pytest.raises(GeneratedTestPolicyError, match="prohibited builtin"):
        GeneratedTestPolicy().validate(
            tmp_path,
            GeneratedTestProposal(
                "tests/test_eval.py",
                "def test_eval():\n    assert eval('1') == 1\n",
                "x",
                "x",
            ),
        )
    with pytest.raises(GeneratedTestPolicyError, match="dunder"):
        GeneratedTestPolicy().validate(
            tmp_path,
            GeneratedTestProposal(
                "tests/test_dunder.py",
                "def test_dunder():\n    assert object.__subclasses__()\n",
                "x",
                "x",
            ),
        )


def test_source_context_enforces_path_count_and_character_bounds(tmp_path: Path) -> None:
    """Only explicit local Python files enter the model context."""
    source = tmp_path / "module.py"
    source.write_text("x = 1\n", encoding="utf-8")

    assert collect_source_context(tmp_path, ("module.py",)) == {"module.py": "x = 1\n"}
    with pytest.raises(SourceContextError, match="exceeds"):
        collect_source_context(tmp_path, tuple(f"file_{index}.py" for index in range(5)))
    with pytest.raises(SourceContextError, match="invalid source"):
        collect_source_context(tmp_path, ("../outside.py",))
    with pytest.raises(SourceContextError, match="POSIX"):
        collect_source_context(tmp_path, ("src\\module.py",))


def test_verification_error_removes_test_and_persists_audit_row(tmp_path: Path) -> None:
    """Collection failures are removed while their hash and outcome remain auditable."""
    run = RunClient().execute(
        RunSpec(repository=tmp_path, command=[sys.executable, "-c", "import sys; sys.exit(1)"]),
    )
    diagnostic = DiagnosticResult(
        assessment=RunAssessment(
            summary="failed",
            goal_alignment="violated",
            findings=(),
            needs_investigation=True,
            confidence=1.0,
        ),
        assessment_call_id="critic-call",
    )
    database = tmp_path / ".appmonitor" / "runs.sqlite3"
    workflow = RegressionTestWorkflow(
        generator=RegressionTestGenerator(
            FakeStructuredClient(
                {
                    "path": _proposal().path,
                    "content": _proposal().content,
                    "target_behavior": _proposal().target_behavior,
                    "rationale": _proposal().rationale,
                },
            ),
        ),
        runner=FixedRunner(exit_code=2),
        store=SQLiteRegressionStore(database),
    )

    result = workflow.generate(
        run,
        diagnostic,
        source_paths=(),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    )

    assert result.status == "verification_error"
    assert not (tmp_path / result.proposal.path).exists()
    assert result.to_dict()["content_sha256"] == result.content_sha256
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT path, content_sha256, status, pytest_exit_code "
            "FROM run_regression_tests WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row == (
        result.proposal.path,
        result.content_sha256,
        "verification_error",
        2,
    )


def test_bounded_runner_reports_missing_executable(tmp_path: Path) -> None:
    """Unavailable infrastructure is normalized instead of raised."""
    result = BoundedPytestRunner().run(("definitely-missing-command",), cwd=tmp_path)

    assert result.exit_code == _COMMAND_NOT_FOUND_EXIT_CODE

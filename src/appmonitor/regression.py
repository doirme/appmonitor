"""Policy-constrained regression-test generation and reproduction verification."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from appmonitor.openrouter import ChatMessage
from appmonitor.repository import CommandResult

if TYPE_CHECKING:
    from appmonitor.agents import DiagnosticResult, StructuredLLM
    from appmonitor.openrouter import LLMBudget
    from appmonitor.orchestrator import OrchestratedRun
    from appmonitor.repository import CommandRunner

_MAX_TEST_BYTES = 32 * 1024
_MAX_SOURCE_FILES = 4
_MAX_SOURCE_CHARS = 20_000
_PYTEST_TIMEOUT_SECONDS = 120
_PYTEST_REPRODUCTION_EXIT_CODE = 1
_PYTEST_TIMEOUT_EXIT_CODE = 124
_PROHIBITED_IMPORTS = frozenset(
    {
        "ctypes",
        "httpx",
        "os",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "urllib",
    },
)
_PROHIBITED_CALLS = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "input",
        "open",
        "setattr",
    },
)
_PROHIBITED_METHODS = frozenset(
    {
        "chmod",
        "mkdir",
        "open",
        "rename",
        "replace",
        "rmdir",
        "rmtree",
        "symlink_to",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    },
)

RegressionStatus = Literal["reproduces", "does_not_reproduce", "verification_error"]


class TestPolicyError(ValueError):
    """Raised before writing a generated test that violates local policy."""


class SourceContextError(ValueError):
    """Raised when requested source context escapes deterministic read bounds."""


@dataclass(frozen=True, slots=True)
class TestProposal:
    """One model-proposed regression test before local authorization."""

    path: str
    content: str
    target_behavior: str
    rationale: str

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TestProposal:
        """Build a proposal from schema-validated model data."""
        return cls(
            path=cast("str", data["path"]),
            content=cast("str", data["content"]),
            target_behavior=cast("str", data["target_behavior"]),
            rationale=cast("str", data["rationale"]),
        )


@dataclass(frozen=True, slots=True)
class RegressionTestResult:
    """Verification outcome for one generated test."""

    proposal: TestProposal
    status: RegressionStatus
    pytest_exit_code: int
    stdout: str
    stderr: str

    @property
    def content_sha256(self) -> str:
        """Return the proposal-content digest."""
        return sha256(self.proposal.content.encode()).hexdigest()

    def to_dict(self) -> dict[str, object]:
        """Return portable result data."""
        return {
            "proposal": asdict(self.proposal),
            "status": self.status,
            "pytest_exit_code": self.pytest_exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "content_sha256": self.content_sha256,
        }


class RegressionTestGenerator:
    """Ask an LLM for one test proposal without granting write access."""

    def __init__(self, client: StructuredLLM) -> None:
        """Retain only structured completion capability."""
        self._client = client

    def propose(
        self,
        diagnostic: DiagnosticResult,
        *,
        source_context: dict[str, str],
        budget: LLMBudget,
    ) -> TestProposal:
        """Generate one schema-validated test proposal."""
        payload = {
            "diagnostic": diagnostic.to_dict(),
            "source_files": source_context,
            "policy": {
                "path": "new tests/**/*.py file",
                "one_test_file": True,
                "no_network_process_dynamic_execution_or_file_mutation": True,
            },
        }
        completion = self._client.complete_structured(
            task="regression_test_architect",
            messages=(
                ChatMessage(
                    "system",
                    "Design one minimal failing pytest regression test from the supplied evidence "
                    "and source only. Return code, not a patch. Do not use network, subprocesses, "
                    "dynamic execution, filesystem mutation, skips, or xfails.",
                ),
                ChatMessage("user", json.dumps(payload, sort_keys=True)),
            ),
            schema_name="regression_test_proposal",
            schema=_TEST_PROPOSAL_SCHEMA,
            budget=budget,
            max_output_tokens=2_500,
            max_attempts=2,
        )
        return TestProposal.from_dict(completion.data)


class GeneratedTestPolicy:
    """Authorize only narrow, static, new pytest files."""

    def validate(self, repository: Path, proposal: TestProposal) -> Path:
        """Return the resolved target path or raise before any write."""
        target = _test_target(repository, proposal.path)
        if target.exists():
            message = f"generated test path already exists: {proposal.path}"
            raise TestPolicyError(message)
        if len(proposal.content.encode()) > _MAX_TEST_BYTES:
            message = f"generated test exceeds {_MAX_TEST_BYTES} bytes"
            raise TestPolicyError(message)
        try:
            tree = ast.parse(proposal.content, filename=proposal.path)
        except SyntaxError as error:
            message = f"generated test is invalid Python: {error.msg}"
            raise TestPolicyError(message) from error
        _validate_test_ast(tree)
        return target


class BoundedPytestRunner:
    """Execute one fixed pytest command with a hard two-minute limit."""

    def run(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Run the pre-authorized argument vector without a shell."""
        try:
            completed = subprocess.run(  # noqa: S603 - workflow constructs the fixed command
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_PYTEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(
                _PYTEST_TIMEOUT_EXIT_CODE,
                _decoded_timeout_stream(error.stdout),
                _decoded_timeout_stream(error.stderr),
            )
        except FileNotFoundError as error:
            return CommandResult(127, "", str(error))
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class RegressionTestWorkflow:
    """Generate, authorize, write, and prove one failing regression test."""

    def __init__(
        self,
        *,
        generator: RegressionTestGenerator,
        policy: GeneratedTestPolicy | None = None,
        runner: CommandRunner | None = None,
        store: SQLiteRegressionStore | None = None,
    ) -> None:
        """Inject model, policy, execution, and persistence boundaries."""
        self._generator = generator
        self._policy = policy or GeneratedTestPolicy()
        self._runner = runner or BoundedPytestRunner()
        self._store = store

    def generate(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
    ) -> RegressionTestResult:
        """Keep a generated test only when pytest proves a real failure."""
        source_context = collect_source_context(run.report.repository, source_paths)
        proposal = self._generator.propose(
            diagnostic,
            source_context=source_context,
            budget=budget,
        )
        repository = Path(run.report.repository)
        target = self._policy.validate(repository, proposal)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(proposal.content)
        command = (
            "uv",
            "run",
            "python",
            "-m",
            "pytest",
            "--rootdir=.",
            proposal.path,
            "-q",
        )
        command_result = self._runner.run(command, cwd=repository)
        status: RegressionStatus = (
            "reproduces"
            if command_result.exit_code == _PYTEST_REPRODUCTION_EXIT_CODE
            else "verification_error"
            if command_result.exit_code not in {0, _PYTEST_REPRODUCTION_EXIT_CODE}
            else "does_not_reproduce"
        )
        if status != "reproduces":
            target.unlink()
        result = RegressionTestResult(
            proposal=proposal,
            status=status,
            pytest_exit_code=command_result.exit_code,
            stdout=command_result.stdout[-4_000:],
            stderr=command_result.stderr[-4_000:],
        )
        if self._store:
            self._store.save(run.run_id, result)
        return result


def collect_source_context(
    repository: str | Path,
    source_paths: tuple[str, ...],
) -> dict[str, str]:
    """Read a small explicit set of repository-local Python sources."""
    root = Path(repository).resolve()
    if len(source_paths) > _MAX_SOURCE_FILES:
        message = f"source context exceeds {_MAX_SOURCE_FILES} files"
        raise SourceContextError(message)
    context: dict[str, str] = {}
    remaining = _MAX_SOURCE_CHARS
    for relative in source_paths:
        path = _source_path(root, relative)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            message = f"cannot read source context: {relative}"
            raise SourceContextError(message) from error
        excerpt = content[:remaining]
        context[PurePosixPath(relative).as_posix()] = excerpt
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    return context


_REGRESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_regression_tests (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    target_behavior TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL,
    pytest_exit_code INTEGER NOT NULL,
    PRIMARY KEY (run_id, path)
);
"""


class SQLiteRegressionStore:
    """Persist generated-test audit facts without duplicating source content."""

    def __init__(self, database: str | Path) -> None:
        """Initialize the regression-test table."""
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_REGRESSION_SCHEMA)

    def save(self, run_id: str, result: RegressionTestResult) -> None:
        """Associate one generated test result with its source run."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO run_regression_tests (
                    run_id, path, content_sha256, target_behavior,
                    rationale, status, pytest_exit_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.proposal.path,
                    result.content_sha256,
                    result.proposal.target_behavior,
                    result.proposal.rationale,
                    result.status,
                    result.pytest_exit_code,
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a foreign-key-enforcing connection."""
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _test_target(repository: Path, relative: str) -> Path:
    """Resolve a strict POSIX test path below the repository tests directory."""
    if "\\" in relative:
        message = "generated test path must use POSIX separators"
        raise TestPolicyError(message)
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
        or pure.parts[0] != "tests"
        or pure.suffix != ".py"
    ):
        message = "generated test path must be a relative tests/**/*.py path"
        raise TestPolicyError(message)
    root = repository.resolve()
    tests_root = (root / "tests").resolve()
    target = (root / Path(*pure.parts)).resolve()
    if not target.is_relative_to(tests_root):
        message = "generated test path escapes the tests directory"
        raise TestPolicyError(message)
    current = target.parent
    while current.is_relative_to(tests_root):
        if current.exists() and current.is_symlink():
            message = "generated test path traverses a symbolic link"
            raise TestPolicyError(message)
        if current == tests_root:
            break
        current = current.parent
    return target


def _source_path(repository: Path, relative: str) -> Path:
    """Resolve one explicit Python source path within the repository."""
    if "\\" in relative:
        message = "source paths must use POSIX separators"
        raise SourceContextError(message)
    pure = PurePosixPath(relative)
    target = (repository / Path(*pure.parts)).resolve()
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or pure.suffix != ".py"
        or not target.is_relative_to(repository)
        or not target.is_file()
    ):
        message = f"invalid source context path: {relative}"
        raise SourceContextError(message)
    return target


def _validate_test_ast(tree: ast.Module) -> None:
    """Reject dangerous capabilities and require a meaningful pytest test."""
    has_test = False
    has_assertion = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _validate_import(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_",
        ):
            has_test = True
        if isinstance(node, ast.Assert) or _is_pytest_raises(node):
            has_assertion = True
        if isinstance(node, ast.Call):
            _validate_call(node)
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            message = "generated test accesses a dunder attribute"
            raise TestPolicyError(message)
    if not has_test or not has_assertion:
        message = "generated test must define test_* and contain an assertion or pytest.raises"
        raise TestPolicyError(message)


def _validate_import(node: ast.Import | ast.ImportFrom) -> None:
    """Reject imports that grant external side-effect capabilities."""
    module = (
        node.module.split(".", maxsplit=1)[0]
        if isinstance(node, ast.ImportFrom) and node.module
        else None
    )
    names = (
        {alias.name.split(".", maxsplit=1)[0] for alias in node.names}
        if isinstance(node, ast.Import)
        else {module}
    )
    if names & _PROHIBITED_IMPORTS:
        message = "generated test imports a prohibited capability"
        raise TestPolicyError(message)


def _validate_call(node: ast.Call) -> None:
    """Reject dynamic execution and direct filesystem mutation."""
    if isinstance(node.func, ast.Name) and node.func.id in _PROHIBITED_CALLS:
        message = f"generated test calls prohibited builtin {node.func.id}"
        raise TestPolicyError(message)
    if isinstance(node.func, ast.Attribute) and node.func.attr in _PROHIBITED_METHODS:
        message = f"generated test calls prohibited method {node.func.attr}"
        raise TestPolicyError(message)


def _is_pytest_raises(node: ast.AST) -> bool:
    """Return whether an AST node calls `pytest.raises`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "raises"
    )


def _decoded_timeout_stream(value: str | bytes | None) -> str:
    """Normalize TimeoutExpired partial output."""
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


_TEST_PROPOSAL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "pattern": r"^tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_]+\.py$",
            "maxLength": 300,
        },
        "content": {"type": "string", "minLength": 1, "maxLength": _MAX_TEST_BYTES},
        "target_behavior": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 1_000},
    },
    "required": ["path", "content", "target_behavior", "rationale"],
    "additionalProperties": False,
}

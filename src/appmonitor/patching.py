"""Bounded source patching with transactional rollback and independent review."""

from __future__ import annotations

import ast
import difflib
import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Self, cast

from appmonitor.openrouter import ChatMessage
from appmonitor.regression import BoundedPytestRunner, collect_source_context

if TYPE_CHECKING:
    from types import TracebackType

    from appmonitor.agents import DiagnosticResult, StructuredLLM
    from appmonitor.openrouter import LLMBudget
    from appmonitor.orchestrator import OrchestratedRun
    from appmonitor.regression import RegressionTestResult
    from appmonitor.repository import CommandRunner

_MAX_PATCH_FILES = 3
_MAX_CHANGED_LINES = 200
_MAX_FILE_BYTES = 64 * 1024
_COMPILE_EXCLUDE = r"(?:^|[\\/])(?:\.venv|\.appmonitor|__pycache__)(?:[\\/]|$)"

PatchRisk = Literal["low", "medium", "high"]
ReviewVerdict = Literal["approve", "reject"]
PatchStatus = Literal["applied", "rejected"]


class PatchPolicyError(ValueError):
    """Raised before mutation when a proposal exceeds its explicit scope."""


@dataclass(frozen=True, slots=True)
class PatchFilePlan:
    """One source file selected by the read-only planner."""

    path: str
    rationale: str

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PatchFilePlan:
        """Build one planned file from schema-validated data."""
        return cls(path=cast("str", data["path"]), rationale=cast("str", data["rationale"]))


@dataclass(frozen=True, slots=True)
class PatchPlan:
    """Bounded ordered intent before implementation."""

    summary: str
    files: tuple[PatchFilePlan, ...]
    risk: PatchRisk
    acceptance_criteria: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PatchPlan:
        """Build a plan from schema-validated data."""
        raw_files = cast("list[dict[str, object]]", data["files"])
        return cls(
            summary=cast("str", data["summary"]),
            files=tuple(PatchFilePlan.from_dict(item) for item in raw_files),
            risk=cast("PatchRisk", data["risk"]),
            acceptance_criteria=tuple(cast("list[str]", data["acceptance_criteria"])),
        )

    def to_dict(self) -> dict[str, object]:
        """Return portable plan data."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FileReplacement:
    """Full replacement of one exact existing source version."""

    path: str
    original_sha256: str
    content: str
    rationale: str

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> FileReplacement:
        """Build one replacement from schema-validated data."""
        return cls(
            path=cast("str", data["path"]),
            original_sha256=cast("str", data["original_sha256"]),
            content=cast("str", data["content"]),
            rationale=cast("str", data["rationale"]),
        )


@dataclass(frozen=True, slots=True)
class PatchProposal:
    """One implementation proposal before local authorization."""

    summary: str
    replacements: tuple[FileReplacement, ...]

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PatchProposal:
        """Build a proposal from schema-validated data."""
        raw_replacements = cast("list[dict[str, object]]", data["replacements"])
        return cls(
            summary=cast("str", data["summary"]),
            replacements=tuple(FileReplacement.from_dict(item) for item in raw_replacements),
        )


@dataclass(frozen=True, slots=True)
class AuthorizedChange:
    """Locally authorized exact byte replacement."""

    path: str
    target: Path
    original: bytes
    replacement: bytes
    diff: str
    changed_lines: int


@dataclass(frozen=True, slots=True)
class AuthorizedPatch:
    """Immutable set of authorized changes."""

    summary: str
    changes: tuple[AuthorizedChange, ...]

    @property
    def diff(self) -> str:
        """Join file diffs for review and persistence."""
        return "\n".join(change.diff for change in self.changes)

    @property
    def sha256(self) -> str:
        """Hash the complete authorized diff."""
        return sha256(self.diff.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """One deterministic fixed-command validation result."""

    name: str
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class PatchValidation:
    """Ordered validation results, stopping at first failure."""

    checks: tuple[ValidationCheck, ...]

    @property
    def passed(self) -> bool:
        """Return whether all required checks completed successfully."""
        return len(self.checks) == len(_validation_commands("test.py")) and all(
            check.exit_code == 0 for check in self.checks
        )

    def to_dict(self) -> dict[str, object]:
        """Return portable validation data."""
        return {"passed": self.passed, "checks": [asdict(check) for check in self.checks]}


@dataclass(frozen=True, slots=True)
class PatchReview:
    """Independent structured judgment after deterministic verification."""

    verdict: ReviewVerdict
    summary: str
    findings: tuple[str, ...]
    confidence: float

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PatchReview:
        """Build a review from schema-validated data."""
        return cls(
            verdict=cast("ReviewVerdict", data["verdict"]),
            summary=cast("str", data["summary"]),
            findings=tuple(cast("list[str]", data["findings"])),
            confidence=float(cast("float", data["confidence"])),
        )

    def to_dict(self) -> dict[str, object]:
        """Return portable review data."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PatchPipelineResult:
    """Final local patch state and complete verification evidence."""

    status: PatchStatus
    reason: str
    plan: PatchPlan
    patch_sha256: str
    diff: str
    validation: PatchValidation
    review: PatchReview | None

    def to_dict(self) -> dict[str, object]:
        """Return portable pipeline data."""
        return {
            "status": self.status,
            "reason": self.reason,
            "plan": self.plan.to_dict(),
            "patch_sha256": self.patch_sha256,
            "diff": self.diff,
            "validation": self.validation.to_dict(),
            "review": self.review.to_dict() if self.review else None,
        }


class PatchPlannerAgent:
    """Create a read-only bounded plan from diagnosis and reproducing test."""

    def __init__(self, client: StructuredLLM) -> None:
        """Retain only the structured LLM capability."""
        self._client = client

    def plan(
        self,
        diagnostic: DiagnosticResult,
        regression: RegressionTestResult,
        *,
        source_context: dict[str, str],
        budget: LLMBudget,
    ) -> PatchPlan:
        """Return a schema-validated plan limited to supplied source files."""
        payload = {
            "diagnostic": diagnostic.to_dict(),
            "regression": regression.to_dict(),
            "source_files": source_context,
            "limits": {"max_files": _MAX_PATCH_FILES, "existing_python_only": True},
        }
        completion = self._client.complete_structured(
            task="patch_planner",
            messages=(
                ChatMessage(
                    "system",
                    "Plan the smallest correction using only supplied existing Python files. "
                    "Do not return code or commands.",
                ),
                ChatMessage("user", json.dumps(payload, sort_keys=True)),
            ),
            schema_name="patch_plan",
            schema=_patch_plan_schema(tuple(source_context)),
            budget=budget,
            max_output_tokens=1_200,
            max_attempts=2,
        )
        return PatchPlan.from_dict(completion.data)


class PatchImplementerAgent:
    """Propose exact full-file replacements without filesystem access."""

    def __init__(self, client: StructuredLLM) -> None:
        """Retain only the structured LLM capability."""
        self._client = client

    def implement(
        self,
        plan: PatchPlan,
        *,
        source_context: dict[str, str],
        source_hashes: dict[str, str],
        budget: LLMBudget,
    ) -> PatchProposal:
        """Return replacements bound to the SHA-256 of each supplied source."""
        sources = {
            path: {"content": content, "sha256": source_hashes[path]}
            for path, content in source_context.items()
        }
        completion = self._client.complete_structured(
            task="patch_implementer",
            messages=(
                ChatMessage(
                    "system",
                    "Implement only the approved plan. Return complete replacement content for "
                    "existing planned Python files and copy each supplied original SHA-256.",
                ),
                ChatMessage(
                    "user",
                    json.dumps({"plan": plan.to_dict(), "sources": sources}, sort_keys=True),
                ),
            ),
            schema_name="patch_proposal",
            schema=_patch_proposal_schema(tuple(item.path for item in plan.files)),
            budget=budget,
            max_output_tokens=6_000,
            max_attempts=2,
        )
        return PatchProposal.from_dict(completion.data)


class PatchReviewerAgent:
    """Review only the authorized diff and deterministic validation results."""

    def __init__(self, client: StructuredLLM) -> None:
        """Retain an isolated structured-review capability."""
        self._client = client

    def review(
        self,
        plan: PatchPlan,
        patch: AuthorizedPatch,
        validation: PatchValidation,
        *,
        budget: LLMBudget,
    ) -> PatchReview:
        """Return a separate approval or rejection."""
        completion = self._client.complete_structured(
            task="patch_reviewer",
            messages=(
                ChatMessage(
                    "system",
                    "Independently review the bounded diff against the plan and validation. "
                    "Reject scope creep, unsupported behavior, or unresolved risk.",
                ),
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "plan": plan.to_dict(),
                            "diff": patch.diff,
                            "validation": validation.to_dict(),
                        },
                        sort_keys=True,
                    ),
                ),
            ),
            schema_name="patch_review",
            schema=_PATCH_REVIEW_SCHEMA,
            budget=budget,
            max_output_tokens=1_200,
            max_attempts=2,
        )
        return PatchReview.from_dict(completion.data)


@dataclass(frozen=True, slots=True)
class PatchPolicy:
    """Local limits authorizing an exact patch proposal."""

    max_files: int = _MAX_PATCH_FILES
    max_changed_lines: int = _MAX_CHANGED_LINES
    max_file_bytes: int = _MAX_FILE_BYTES

    def authorize(
        self,
        repository: Path,
        plan: PatchPlan,
        proposal: PatchProposal,
        *,
        allowed_source_paths: tuple[str, ...],
    ) -> AuthorizedPatch:
        """Validate scope, hashes, syntax, and diff size before mutation."""
        if not plan.files or len(plan.files) > self.max_files:
            message = f"patch plan must contain between 1 and {self.max_files} files"
            raise PatchPolicyError(message)
        planned_paths = tuple(item.path for item in plan.files)
        if len(set(planned_paths)) != len(planned_paths):
            message = "patch plan contains duplicate paths"
            raise PatchPolicyError(message)
        allowed = set(allowed_source_paths)
        for path in planned_paths:
            if path not in allowed:
                message = f"planned path is outside explicit source scope: {path}"
                raise PatchPolicyError(message)
            _patch_source_target(repository, path)
        if not proposal.replacements or len(proposal.replacements) > self.max_files:
            message = "patch proposal must contain at least one bounded replacement"
            raise PatchPolicyError(message)
        replacement_paths = tuple(item.path for item in proposal.replacements)
        if len(set(replacement_paths)) != len(replacement_paths):
            message = "patch proposal contains duplicate paths"
            raise PatchPolicyError(message)
        changes = tuple(
            self._authorize_replacement(repository, replacement, set(planned_paths))
            for replacement in proposal.replacements
        )
        total_changed = sum(change.changed_lines for change in changes)
        if total_changed > self.max_changed_lines:
            message = f"patch changes {total_changed} lines; limit is {self.max_changed_lines}"
            raise PatchPolicyError(message)
        return AuthorizedPatch(summary=proposal.summary, changes=changes)

    def _authorize_replacement(
        self,
        repository: Path,
        replacement: FileReplacement,
        planned_paths: set[str],
    ) -> AuthorizedChange:
        """Authorize one replacement against its exact current bytes."""
        if replacement.path not in planned_paths:
            message = f"replacement path was not declared by plan: {replacement.path}"
            raise PatchPolicyError(message)
        target = _patch_source_target(repository, replacement.path)
        original = target.read_bytes()
        if sha256(original).hexdigest() != replacement.original_sha256:
            message = f"original hash mismatch for {replacement.path}"
            raise PatchPolicyError(message)
        encoded = replacement.content.encode()
        if len(encoded) > self.max_file_bytes:
            message = f"replacement exceeds byte limit for {replacement.path}"
            raise PatchPolicyError(message)
        if encoded == original:
            message = f"replacement does not change {replacement.path}"
            raise PatchPolicyError(message)
        try:
            ast.parse(replacement.content, filename=replacement.path)
        except SyntaxError as error:
            message = f"replacement is invalid Python for {replacement.path}: {error.msg}"
            raise PatchPolicyError(message) from error
        original_text = _decode_source(original, replacement.path)
        diff, changed_lines = _diff(replacement.path, original_text, replacement.content)
        return AuthorizedChange(
            path=replacement.path,
            target=target,
            original=original,
            replacement=encoded,
            diff=diff,
            changed_lines=changed_lines,
        )


class PatchTransaction:
    """Applied patch that rolls back unless explicitly accepted."""

    def __init__(self, patch: AuthorizedPatch) -> None:
        """Store patch bytes without mutating yet."""
        self._patch = patch
        self._committed = False
        self._applied: list[AuthorizedChange] = []

    def __enter__(self) -> Self:
        """Atomically apply every authorized file."""
        try:
            for change in self._patch.changes:
                _atomic_write(change.target, change.replacement)
                self._applied.append(change)
        except OSError:
            self._rollback()
            raise
        return self

    def commit(self) -> None:
        """Keep applied bytes when the surrounding pipeline succeeds."""
        self._committed = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Restore originals after rejection or any exception."""
        del exc_type, exc_value, traceback
        if not self._committed:
            self._rollback()

    def _rollback(self) -> None:
        """Restore applied files in reverse order."""
        for change in reversed(self._applied):
            _atomic_write(change.target, change.original)
        self._applied.clear()


class AtomicPatchApplier:
    """Create rollback-by-default transactions."""

    def apply(self, patch: AuthorizedPatch) -> PatchTransaction:
        """Return an unapplied transaction context."""
        return PatchTransaction(patch)


class PatchVerifier:
    """Run the immutable regression and project quality gate."""

    def __init__(self, runner: CommandRunner | None = None) -> None:
        """Use the bounded no-shell runner by default."""
        self._runner = runner or BoundedPytestRunner()

    def verify(self, repository: Path, regression_path: str) -> PatchValidation:
        """Run checks in order and stop at the first failure."""
        checks: list[ValidationCheck] = []
        for name, command in _validation_commands(regression_path):
            result = self._runner.run(command, cwd=repository)
            checks.append(
                ValidationCheck(
                    name=name,
                    command=command,
                    exit_code=result.exit_code,
                    stdout=result.stdout[-4_000:],
                    stderr=result.stderr[-4_000:],
                ),
            )
            if result.exit_code != 0:
                break
        return PatchValidation(tuple(checks))


class PatchPipeline:
    """Coordinate plan, bounded mutation, validation, review, and rollback."""

    def __init__(  # noqa: PLR0913 - security boundaries stay independently injectable
        self,
        *,
        planner: PatchPlannerAgent,
        implementer: PatchImplementerAgent,
        reviewer: PatchReviewerAgent,
        policy: PatchPolicy | None = None,
        applier: AtomicPatchApplier | None = None,
        runner: CommandRunner | None = None,
        store: SQLitePatchStore | None = None,
    ) -> None:
        """Inject every model and deterministic authority."""
        self._planner = planner
        self._implementer = implementer
        self._reviewer = reviewer
        self._policy = policy or PatchPolicy()
        self._applier = applier or AtomicPatchApplier()
        self._verifier = PatchVerifier(runner)
        self._store = store

    def execute(
        self,
        run: OrchestratedRun,
        diagnostic: DiagnosticResult,
        regression: RegressionTestResult,
        *,
        source_paths: tuple[str, ...],
        budget: LLMBudget,
    ) -> PatchPipelineResult:
        """Apply a patch locally only after deterministic and independent approval."""
        if regression.status != "reproduces":
            message = "bounded patching requires a proven reproducing test"
            raise PatchPolicyError(message)
        if not source_paths:
            message = "bounded patching requires an explicit non-empty source scope"
            raise PatchPolicyError(message)
        repository = Path(run.report.repository)
        source_context = collect_source_context(repository, source_paths)
        plan = self._planner.plan(
            diagnostic,
            regression,
            source_context=source_context,
            budget=budget,
        )
        proposal = self._implementer.implement(
            plan,
            source_context=source_context,
            source_hashes={
                path: sha256(_patch_source_target(repository, path).read_bytes()).hexdigest()
                for path in source_paths
            },
            budget=budget,
        )
        patch = self._policy.authorize(
            repository,
            plan,
            proposal,
            allowed_source_paths=source_paths,
        )
        review: PatchReview | None = None
        with self._applier.apply(patch) as transaction:
            validation = self._verifier.verify(repository, regression.proposal.path)
            if validation.passed:
                review = self._reviewer.review(plan, patch, validation, budget=budget)
            accepted = validation.passed and review is not None and review.verdict == "approve"
            if accepted:
                transaction.commit()
        result = PatchPipelineResult(
            status="applied" if accepted else "rejected",
            reason=(
                "validated and independently approved"
                if accepted
                else "validation failed"
                if not validation.passed
                else "review rejected"
            ),
            plan=plan,
            patch_sha256=patch.sha256,
            diff=patch.diff,
            validation=validation,
            review=review,
        )
        if self._store:
            self._store.save(run.run_id, result)
        return result


_PATCH_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_patches (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    patch_sha256 TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    review_json TEXT
);
"""


class SQLitePatchStore:
    """Persist complete local patch and verification evidence."""

    def __init__(self, database: str | Path) -> None:
        """Initialize the patch audit table."""
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_PATCH_STORE_SCHEMA)

    def save(self, run_id: str, result: PatchPipelineResult) -> None:
        """Persist one final patch decision."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO run_patches (
                    run_id, status, reason, patch_sha256, plan_json,
                    diff_text, validation_json, review_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.status,
                    result.reason,
                    result.patch_sha256,
                    json.dumps(result.plan.to_dict(), sort_keys=True),
                    result.diff,
                    json.dumps(result.validation.to_dict(), sort_keys=True),
                    json.dumps(result.review.to_dict(), sort_keys=True) if result.review else None,
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a foreign-key-enforcing connection."""
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _patch_source_target(repository: Path, relative: str) -> Path:
    """Resolve one existing non-test Python source without symlink traversal."""
    if "\\" in relative:
        message = "patch source paths must use POSIX separators"
        raise PatchPolicyError(message)
    pure = PurePosixPath(relative)
    root = repository.resolve()
    target = (root / Path(*pure.parts)).resolve()
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or pure.suffix != ".py"
        or not pure.parts
        or pure.parts[0] == "tests"
        or not target.is_relative_to(root)
        or not target.is_file()
    ):
        message = f"patch path must be an existing repository source .py file: {relative}"
        raise PatchPolicyError(message)
    current = target
    while current != root:
        if current.is_symlink():
            message = f"patch path traverses a symbolic link: {relative}"
            raise PatchPolicyError(message)
        current = current.parent
    return target


def _decode_source(content: bytes, path: str) -> str:
    """Decode an existing Python source as UTF-8."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        message = f"patch source is not UTF-8: {path}"
        raise PatchPolicyError(message) from error


def _diff(path: str, original: str, replacement: str) -> tuple[str, int]:
    """Return a unified diff and deterministic changed-line count."""
    original_lines = original.splitlines(keepends=True)
    replacement_lines = replacement.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=original_lines, b=replacement_lines, autojunk=False)
    changed = sum(
        (original_end - original_start) + (replacement_end - replacement_start)
        for (
            tag,
            original_start,
            original_end,
            replacement_start,
            replacement_end,
        ) in matcher.get_opcodes()
        if tag != "equal"
    )
    diff = "".join(
        difflib.unified_diff(
            original_lines,
            replacement_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        ),
    )
    return diff, changed


def _atomic_write(target: Path, content: bytes) -> None:
    """Replace one file atomically while preserving its permission bits."""
    mode = stat.S_IMODE(target.stat().st_mode)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=".appmonitor-patch-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(mode)
        temporary_path.replace(target)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def _validation_commands(regression_path: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the immutable post-patch quality gate."""
    return (
        (
            "regression",
            (
                "uv",
                "run",
                "python",
                "-m",
                "pytest",
                "--rootdir=.",
                regression_path,
                "-q",
            ),
        ),
        (
            "tests",
            ("uv", "run", "python", "-m", "pytest", "--rootdir=.", "-q"),
        ),
        ("ruff", ("uv", "run", "ruff", "check", ".")),
        ("mypy", ("uv", "run", "mypy", ".")),
        (
            "compileall",
            (
                "uv",
                "run",
                "python",
                "-m",
                "compileall",
                "-q",
                "-x",
                _COMPILE_EXCLUDE,
                ".",
            ),
        ),
    )


_PATCH_FILE_PLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 300},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 1_000},
    },
    "required": ["path", "rationale"],
    "additionalProperties": False,
}

_PATCH_PLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "files": {
            "type": "array",
            "items": _PATCH_FILE_PLAN_SCHEMA,
            "minItems": 1,
            "maxItems": _MAX_PATCH_FILES,
        },
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "acceptance_criteria": {
            "type": "array",
            "items": {"type": "string", "maxLength": 500},
            "minItems": 1,
            "maxItems": 10,
        },
    },
    "required": ["summary", "files", "risk", "acceptance_criteria"],
    "additionalProperties": False,
}

_FILE_REPLACEMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "maxLength": 300},
        "original_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "content": {"type": "string", "minLength": 1, "maxLength": _MAX_FILE_BYTES},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 1_000},
    },
    "required": ["path", "original_sha256", "content", "rationale"],
    "additionalProperties": False,
}

_PATCH_PROPOSAL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "replacements": {
            "type": "array",
            "items": _FILE_REPLACEMENT_SCHEMA,
            "minItems": 1,
            "maxItems": _MAX_PATCH_FILES,
        },
    },
    "required": ["summary", "replacements"],
    "additionalProperties": False,
}

_PATCH_REVIEW_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "findings": {
            "type": "array",
            "items": {"type": "string", "maxLength": 1_000},
            "maxItems": 12,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["verdict", "summary", "findings", "confidence"],
    "additionalProperties": False,
}


def _patch_plan_schema(allowed_paths: tuple[str, ...]) -> dict[str, object]:
    """Bind planner file paths to the explicit source context."""
    file_schema = {
        **_PATCH_FILE_PLAN_SCHEMA,
        "properties": {
            **cast("dict[str, object]", _PATCH_FILE_PLAN_SCHEMA["properties"]),
            "path": {"type": "string", "enum": list(allowed_paths)},
        },
    }
    return {
        **_PATCH_PLAN_SCHEMA,
        "properties": {
            **cast("dict[str, object]", _PATCH_PLAN_SCHEMA["properties"]),
            "files": {
                "type": "array",
                "items": file_schema,
                "minItems": 1,
                "maxItems": min(_MAX_PATCH_FILES, len(allowed_paths)),
            },
        },
    }


def _patch_proposal_schema(planned_paths: tuple[str, ...]) -> dict[str, object]:
    """Bind implementation replacements to planner-authorized paths."""
    replacement_schema = {
        **_FILE_REPLACEMENT_SCHEMA,
        "properties": {
            **cast("dict[str, object]", _FILE_REPLACEMENT_SCHEMA["properties"]),
            "path": {"type": "string", "enum": list(planned_paths)},
        },
    }
    return {
        **_PATCH_PROPOSAL_SCHEMA,
        "properties": {
            **cast("dict[str, object]", _PATCH_PROPOSAL_SCHEMA["properties"]),
            "replacements": {
                "type": "array",
                "items": replacement_schema,
                "minItems": 1,
                "maxItems": len(planned_paths),
            },
        },
    }

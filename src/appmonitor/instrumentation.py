"""Optional bounded in-process instrumentation for Python callables."""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from functools import wraps
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, ParamSpec, Protocol, TypeVar, cast
from uuid import uuid4

import psutil

from appmonitor.artifacts import Artifact, ArtifactChanges, compare_snapshots, snapshot_files

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

P = ParamSpec("P")
R = TypeVar("R")
CallOutcome = Literal["returned", "raised"]

_MAX_VALUE_CHARS = 500
_SECRET_NAME = re.compile(r"(?i)(api[_-]?key|token|password|secret)")
_SECRET_VALUE = re.compile(r"(?i)\bsk-(?:or-)?[a-z0-9_-]{8,}")


@dataclass(frozen=True, slots=True)
class OutputArtifact:
    """Declare one expected repository output pattern."""

    pattern: str
    required: bool = True

    def __post_init__(self) -> None:
        """Reject empty patterns."""
        if not self.pattern.strip():
            message = "output artifact pattern must not be empty"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Optional wall-time and RSS-delta limits for one call."""

    max_runtime_seconds: float | None = None
    max_memory_delta_mb: float | None = None

    def __post_init__(self) -> None:
        """Reject non-positive configured limits."""
        values = (self.max_runtime_seconds, self.max_memory_delta_mb)
        if any(value is not None and value <= 0 for value in values):
            message = "resource budget limits must be greater than zero"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class OutputCheck:
    """Result of matching one declared output against changed artifacts."""

    pattern: str
    required: bool
    matches: tuple[str, ...]
    passed: bool


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    """Result of comparing one resource observation with its limit."""

    name: str
    limit: float
    observed: float
    passed: bool


@dataclass(frozen=True, slots=True)
class CallReference:
    """Stable facts copied from an earlier monitored call."""

    duration_seconds: float
    rss_delta_bytes: int
    return_sha256: str | None

    @classmethod
    def from_observation(cls, observation: CallObservation) -> CallReference:
        """Create a comparison baseline from one completed observation."""
        return cls(
            duration_seconds=observation.duration_seconds,
            rss_delta_bytes=observation.rss_delta_bytes,
            return_sha256=observation.return_sha256,
        )


@dataclass(frozen=True, slots=True)
class ReferenceComparison:
    """Current-call differences relative to an explicit reference."""

    duration_ratio: float | None
    memory_delta_ratio: float | None
    return_matches: bool | None


@dataclass(frozen=True, slots=True)
class CallObservation:
    """Bounded portable facts for one instrumented Python call."""

    call_id: str
    function: str
    goal: str
    started_at: datetime
    duration_seconds: float
    arguments: Mapping[str, str]
    outcome: CallOutcome
    return_type: str | None
    return_sha256: str | None
    exception_type: str | None
    exception_message: str | None
    rss_delta_bytes: int
    artifacts: ArtifactChanges
    output_checks: tuple[OutputCheck, ...]
    budget_checks: tuple[BudgetCheck, ...]
    reference_comparison: ReferenceComparison | None


class CallRecorder(Protocol):
    """Narrow destination for completed call observations."""

    def record(self, observation: CallObservation) -> None:
        """Retain one immutable observation."""


class InMemoryCallRecorder:
    """Collect observations in insertion order for local use and tests."""

    def __init__(self) -> None:
        """Create an empty recorder."""
        self._records: list[CallObservation] = []

    @property
    def records(self) -> tuple[CallObservation, ...]:
        """Return an immutable view of recorded calls."""
        return tuple(self._records)

    def record(self, observation: CallObservation) -> None:
        """Append one completed call."""
        self._records.append(observation)


class _NullCallRecorder:
    """Discard observations when no destination is configured."""

    def record(self, observation: CallObservation) -> None:
        """Discard one record."""
        del observation


_SCHEMA = """
CREATE TABLE IF NOT EXISTS instrumented_calls (
    call_id TEXT PRIMARY KEY,
    function TEXT NOT NULL,
    goal TEXT NOT NULL,
    started_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    observation_json TEXT NOT NULL
);
"""


class SQLiteInstrumentationStore:
    """Persist bounded call observations as portable JSON."""

    def __init__(self, database: str | Path) -> None:
        """Initialize the instrumentation table."""
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(_SCHEMA)

    def record(self, observation: CallObservation) -> None:
        """Insert one immutable call observation."""
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                """
                INSERT INTO instrumented_calls (
                    call_id, function, goal, started_at, outcome, observation_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.call_id,
                    observation.function,
                    observation.goal,
                    observation.started_at.isoformat(),
                    observation.outcome,
                    json.dumps(_observation_to_dict(observation), sort_keys=True),
                ),
            )

    def list_records(self) -> tuple[CallObservation, ...]:
        """Return observations in insertion order."""
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT observation_json FROM instrumented_calls ORDER BY rowid",
            ).fetchall()
        return tuple(_observation_from_dict(json.loads(row[0])) for row in rows)


def monitored(  # noqa: PLR0913 - public instrumentation choices stay explicit
    *,
    goal: str,
    outputs: Sequence[OutputArtifact] = (),
    budget: ResourceBudget | None = None,
    repository: str | Path | None = None,
    recorder: CallRecorder | None = None,
    reference: CallReference | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Observe calls without changing their return or exception behavior."""
    if not goal.strip():
        message = "monitored goal must not be empty"
        raise ValueError(message)
    root = Path(repository).resolve() if repository is not None else None
    if root is not None and not root.is_dir():
        message = f"instrumentation repository is not a directory: {root}"
        raise ValueError(message)
    destination = recorder or _NullCallRecorder()
    declared_outputs = tuple(outputs)

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)
        qualified_name = f"{function.__module__}.{function.__qualname__}"

        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            started_at = datetime.now(UTC)
            started = time.perf_counter()
            before_rss = psutil.Process(os.getpid()).memory_info().rss
            before_artifacts = snapshot_files(root) if root else {}
            arguments = _arguments(signature, args, kwargs)
            outcome: CallOutcome = "returned"
            return_type: str | None = None
            return_sha256: str | None = None
            exception_type: str | None = None
            exception_message: str | None = None
            try:
                result = function(*args, **kwargs)
                return_type = type(result).__name__
                return_sha256 = sha256(_bounded_repr(result).encode()).hexdigest()
            except Exception as error:
                outcome = "raised"
                exception_type = type(error).__name__
                exception_message = _redact(_bounded_repr(error))
                raise
            else:
                return result
            finally:
                duration = max(time.perf_counter() - started, 1e-9)
                rss_delta = psutil.Process(os.getpid()).memory_info().rss - before_rss
                artifacts = (
                    compare_snapshots(before_artifacts, snapshot_files(root))
                    if root
                    else ArtifactChanges()
                )
                destination.record(
                    CallObservation(
                        call_id=str(uuid4()),
                        function=qualified_name,
                        goal=goal,
                        started_at=started_at,
                        duration_seconds=duration,
                        arguments=arguments,
                        outcome=outcome,
                        return_type=return_type,
                        return_sha256=return_sha256,
                        exception_type=exception_type,
                        exception_message=exception_message,
                        rss_delta_bytes=rss_delta,
                        artifacts=artifacts,
                        output_checks=_output_checks(declared_outputs, artifacts),
                        budget_checks=_budget_checks(budget, duration, rss_delta),
                        reference_comparison=_compare_reference(
                            reference,
                            duration,
                            rss_delta,
                            return_sha256,
                        ),
                    ),
                )

        return wrapped

    return decorate


def _arguments(
    signature: inspect.Signature,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> dict[str, str]:
    """Bind arguments and redact values with secret-bearing parameter names."""
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    return {
        name: "<redacted>" if _SECRET_NAME.search(name) else _redact(_bounded_repr(value))
        for name, value in bound.arguments.items()
    }


def _bounded_repr(value: object) -> str:
    """Return a bounded best-effort representation."""
    try:
        rendered = repr(value)
    except Exception:  # noqa: BLE001 - arbitrary user objects may fail during repr
        rendered = f"<unrepresentable {type(value).__name__}>"
    return rendered[:_MAX_VALUE_CHARS]


def _redact(value: str) -> str:
    """Replace common OpenRouter-style secret values."""
    return _SECRET_VALUE.sub("<redacted>", value)


def _output_checks(
    outputs: tuple[OutputArtifact, ...],
    artifacts: ArtifactChanges,
) -> tuple[OutputCheck, ...]:
    """Match declared outputs against created and modified artifact paths."""
    changed = tuple(item.path for item in (*artifacts.created, *artifacts.modified))
    return tuple(
        OutputCheck(
            pattern=output.pattern,
            required=output.required,
            matches=tuple(path for path in changed if fnmatch(path, output.pattern)),
            passed=not output.required or any(fnmatch(path, output.pattern) for path in changed),
        )
        for output in outputs
    )


def _budget_checks(
    budget: ResourceBudget | None,
    duration_seconds: float,
    rss_delta_bytes: int,
) -> tuple[BudgetCheck, ...]:
    """Evaluate configured limits without changing function behavior."""
    if budget is None:
        return ()
    checks: list[BudgetCheck] = []
    if budget.max_runtime_seconds is not None:
        checks.append(
            BudgetCheck(
                "max_runtime_seconds",
                budget.max_runtime_seconds,
                duration_seconds,
                duration_seconds <= budget.max_runtime_seconds,
            ),
        )
    if budget.max_memory_delta_mb is not None:
        observed_mb = max(0, rss_delta_bytes) / (1024 * 1024)
        checks.append(
            BudgetCheck(
                "max_memory_delta_mb",
                budget.max_memory_delta_mb,
                observed_mb,
                observed_mb <= budget.max_memory_delta_mb,
            ),
        )
    return tuple(checks)


def _compare_reference(
    reference: CallReference | None,
    duration_seconds: float,
    rss_delta_bytes: int,
    return_sha256: str | None,
) -> ReferenceComparison | None:
    """Compare current facts with a caller-selected prior observation."""
    if reference is None:
        return None
    return ReferenceComparison(
        duration_ratio=(
            duration_seconds / reference.duration_seconds
            if reference.duration_seconds > 0
            else None
        ),
        memory_delta_ratio=(
            max(0, rss_delta_bytes) / reference.rss_delta_bytes
            if reference.rss_delta_bytes > 0
            else None
        ),
        return_matches=(
            return_sha256 == reference.return_sha256
            if return_sha256 is not None and reference.return_sha256 is not None
            else None
        ),
    )


def _observation_to_dict(observation: CallObservation) -> dict[str, object]:
    """Convert an observation to JSON-compatible data."""
    payload = asdict(observation)
    payload["started_at"] = observation.started_at.isoformat()
    return payload


def _observation_from_dict(data: dict[str, object]) -> CallObservation:
    """Reconstruct one observation from validated internal JSON."""
    artifacts = cast("dict[str, list[dict[str, object]]]", data["artifacts"])
    raw_comparison = cast("dict[str, object] | None", data["reference_comparison"])
    return CallObservation(
        call_id=cast("str", data["call_id"]),
        function=cast("str", data["function"]),
        goal=cast("str", data["goal"]),
        started_at=datetime.fromisoformat(cast("str", data["started_at"])),
        duration_seconds=float(cast("float", data["duration_seconds"])),
        arguments=cast("dict[str, str]", data["arguments"]),
        outcome=cast("CallOutcome", data["outcome"]),
        return_type=cast("str | None", data["return_type"]),
        return_sha256=cast("str | None", data["return_sha256"]),
        exception_type=cast("str | None", data["exception_type"]),
        exception_message=cast("str | None", data["exception_message"]),
        rss_delta_bytes=cast("int", data["rss_delta_bytes"]),
        artifacts=ArtifactChanges(
            created=_artifacts(artifacts["created"]),
            modified=_artifacts(artifacts["modified"]),
            deleted=_artifacts(artifacts["deleted"]),
        ),
        output_checks=tuple(
            OutputCheck(
                pattern=cast("str", item["pattern"]),
                required=cast("bool", item["required"]),
                matches=tuple(cast("list[str]", item["matches"])),
                passed=cast("bool", item["passed"]),
            )
            for item in cast("list[dict[str, object]]", data["output_checks"])
        ),
        budget_checks=tuple(
            BudgetCheck(
                name=cast("str", item["name"]),
                limit=float(cast("float", item["limit"])),
                observed=float(cast("float", item["observed"])),
                passed=cast("bool", item["passed"]),
            )
            for item in cast("list[dict[str, object]]", data["budget_checks"])
        ),
        reference_comparison=(
            ReferenceComparison(
                duration_ratio=cast("float | None", raw_comparison["duration_ratio"]),
                memory_delta_ratio=cast("float | None", raw_comparison["memory_delta_ratio"]),
                return_matches=cast("bool | None", raw_comparison["return_matches"]),
            )
            if raw_comparison
            else None
        ),
    )


def _artifacts(items: list[dict[str, object]]) -> tuple[Artifact, ...]:
    """Reconstruct artifact metadata."""
    return tuple(
        Artifact(
            path=cast("str", item["path"]),
            size_bytes=cast("int", item["size_bytes"]),
            modified_ns=cast("int", item["modified_ns"]),
            sha256=cast("str", item["sha256"]),
        )
        for item in items
    )

"""Strict data-only goal contracts and deterministic evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    from appmonitor.execution import RunReport

CheckStatus = Literal["passed", "failed", "unavailable"]
GoalOutcome = Literal["passed", "partial", "failed"]


class GoalContractError(ValueError):
    """Raised when a goal file does not match the supported schema."""


@dataclass(frozen=True, slots=True)
class GoalContract:
    """Validated version-one success criteria."""

    sha256: str
    exit_code: int | None = None
    required_artifacts: tuple[str, ...] = ()
    stdout_contains: tuple[str, ...] = ()
    stderr_contains: tuple[str, ...] = ()
    max_runtime_seconds: float | None = None
    max_peak_rss_mb: float | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the normalized portable contract."""
        return {"version": 1, **asdict(self)}


@dataclass(frozen=True, slots=True)
class GoalCheck:
    """One deterministic comparison against observed run facts."""

    name: str
    status: CheckStatus
    expected: object
    observed: object


@dataclass(frozen=True, slots=True)
class GoalEvaluation:
    """Aggregate result of evaluating all goal checks."""

    overall: GoalOutcome
    checks: tuple[GoalCheck, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible evaluation."""
        return asdict(self)


def load_goal_contract(path: Path) -> GoalContract:
    """Load and strictly validate a version-one YAML goal file."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        message = f"cannot read goal file {path}: {error}"
        raise GoalContractError(message) from error
    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        message = f"invalid goal YAML: {error}"
        raise GoalContractError(message) from error
    root = _mapping(payload, "goal")
    _keys(root, {"version", "process", "artifacts", "events", "resources"}, "goal")
    if root.get("version") != 1:
        message = "goal.version must be 1"
        raise GoalContractError(message)

    process = _section(root, "process", {"exit_code"})
    artifacts = _section(root, "artifacts", {"required"})
    events = _section(root, "events", {"stdout_contains", "stderr_contains"})
    resources = _section(
        root,
        "resources",
        {"max_runtime_seconds", "max_peak_rss_mb"},
    )
    return GoalContract(
        sha256=sha256(raw).hexdigest(),
        exit_code=_optional_int(process, "exit_code"),
        required_artifacts=_strings(artifacts, "required"),
        stdout_contains=_strings(events, "stdout_contains"),
        stderr_contains=_strings(events, "stderr_contains"),
        max_runtime_seconds=_optional_positive(resources, "max_runtime_seconds"),
        max_peak_rss_mb=_optional_positive(resources, "max_peak_rss_mb"),
    )


class GoalEvaluator:
    """Evaluate a validated goal against one immutable run report."""

    def evaluate(self, contract: GoalContract, report: RunReport) -> GoalEvaluation:
        """Return individual checks and a deterministic aggregate outcome."""
        checks: list[GoalCheck] = []
        if contract.exit_code is not None:
            checks.append(
                _check("process.exit_code", contract.exit_code, report.exit_code),
            )
        changed_paths = tuple(
            artifact.path
            for artifact in (*report.artifacts.created, *report.artifacts.modified)
        )
        checks.extend(
            _check(
                f"artifacts.required:{pattern}",
                pattern,
                next((path for path in changed_paths if fnmatch(path, pattern)), None),
            )
            for pattern in contract.required_artifacts
        )
        checks.extend(_event_checks("stdout", contract.stdout_contains, report.stdout))
        checks.extend(_event_checks("stderr", contract.stderr_contains, report.stderr))
        if contract.max_runtime_seconds is not None:
            checks.append(
                _budget_check(
                    "resources.max_runtime_seconds",
                    contract.max_runtime_seconds,
                    report.duration_seconds,
                ),
            )
        if contract.max_peak_rss_mb is not None:
            observed_memory = report.peak_rss_bytes / (1024 * 1024) if report.metrics else None
            checks.append(
                _budget_check(
                    "resources.max_peak_rss_mb",
                    contract.max_peak_rss_mb,
                    observed_memory,
                ),
            )
        statuses = {check.status for check in checks}
        overall: GoalOutcome = "passed"
        if "failed" in statuses:
            overall = "failed"
        elif "unavailable" in statuses:
            overall = "partial"
        return GoalEvaluation(overall=overall, checks=tuple(checks))


def _mapping(value: object, location: str) -> dict[str, Any]:
    """Require a string-keyed YAML mapping."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        message = f"{location} must be a mapping"
        raise GoalContractError(message)
    return cast("dict[str, Any]", value)


def _keys(payload: dict[str, Any], allowed: set[str], location: str) -> None:
    """Reject undocumented fields."""
    unknown = payload.keys() - allowed
    if unknown:
        message = f"unknown {location} field(s): {', '.join(sorted(unknown))}"
        raise GoalContractError(message)


def _section(root: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    """Return and validate one optional mapping section."""
    if name not in root:
        return {}
    section = _mapping(root[name], f"goal.{name}")
    _keys(section, allowed, f"goal.{name}")
    return section


def _optional_int(payload: dict[str, Any], key: str) -> int | None:
    """Read an optional integer without accepting booleans."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"{key} must be an integer"
        raise GoalContractError(message)
    return value


def _optional_positive(payload: dict[str, Any], key: str) -> float | None:
    """Read an optional positive numeric budget."""
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        message = f"{key} must be a positive number"
        raise GoalContractError(message)
    return float(value)


def _strings(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    """Read an optional list of non-empty strings."""
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        message = f"{key} must be a list of non-empty strings"
        raise GoalContractError(message)
    return tuple(value)


def _check(name: str, expected: object, observed: object) -> GoalCheck:
    """Compare an exact or existence-based observation."""
    passed = observed is not None if isinstance(expected, str) else observed == expected
    return GoalCheck(name, "passed" if passed else "failed", expected, observed)


def _event_checks(
    stream_name: str,
    expected_events: tuple[str, ...],
    lines: tuple[Any, ...],
) -> tuple[GoalCheck, ...]:
    """Check required substrings in one captured stream."""
    messages = tuple(line.message for line in lines)
    return tuple(
        GoalCheck(
            f"events.{stream_name}_contains:{event}",
            "passed" if any(event in message for message in messages) else "failed",
            event,
            next((message for message in messages if event in message), None),
        )
        for event in expected_events
    )


def _budget_check(name: str, maximum: float, observed: float | None) -> GoalCheck:
    """Compare an observed value to an inclusive upper bound."""
    if observed is None:
        return GoalCheck(name, "unavailable", maximum, None)
    return GoalCheck(name, "passed" if observed <= maximum else "failed", maximum, observed)

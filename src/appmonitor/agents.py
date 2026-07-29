"""Read-only structured agents for run criticism and incident analysis."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from appmonitor.execution import RunOutcome
from appmonitor.openrouter import ChatMessage

if TYPE_CHECKING:
    from appmonitor.execution import CapturedLine
    from appmonitor.openrouter import LLMBudget, StructuredCompletion
    from appmonitor.orchestrator import OrchestratedRun

_DEFAULT_MAX_LOG_LINES = 40
_DEFAULT_MAX_LINE_CHARS = 500
_SECRET_TOKEN = re.compile(r"(?i)\bsk-(?:or-)?[a-z0-9_-]{8,}")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
)

Severity = Literal["info", "low", "medium", "high", "critical"]
GoalAlignment = Literal["satisfied", "violated", "unknown"]
IncidentClass = Literal[
    "runtime_error",
    "timeout",
    "resource",
    "goal_violation",
    "quality",
    "unknown",
]
Priority = Literal["low", "medium", "high", "critical"]


class StructuredLLM(Protocol):
    """Narrow capability exposed to read-only agents."""

    def complete_structured(  # noqa: PLR0913 - mirrors bounded LLM call contract
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
        """Return locally validated structured data."""


@dataclass(frozen=True, slots=True)
class DiagnosticFinding:
    """One evidence-backed concern identified by the run critic."""

    category: str
    severity: Severity
    summary: str
    evidence: tuple[str, ...]
    recommendation: str
    confidence: float

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DiagnosticFinding:
        """Build a finding from schema-validated data."""
        return cls(
            category=cast("str", data["category"]),
            severity=cast("Severity", data["severity"]),
            summary=cast("str", data["summary"]),
            evidence=tuple(cast("list[str]", data["evidence"])),
            recommendation=cast("str", data["recommendation"]),
            confidence=float(cast("float", data["confidence"])),
        )


@dataclass(frozen=True, slots=True)
class RunAssessment:
    """Read-only judgment of observed execution facts."""

    summary: str
    goal_alignment: GoalAlignment
    findings: tuple[DiagnosticFinding, ...]
    needs_investigation: bool
    confidence: float

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RunAssessment:
        """Build an assessment from schema-validated data."""
        raw_findings = cast("list[dict[str, object]]", data["findings"])
        return cls(
            summary=cast("str", data["summary"]),
            goal_alignment=cast("GoalAlignment", data["goal_alignment"]),
            findings=tuple(DiagnosticFinding.from_dict(item) for item in raw_findings),
            needs_investigation=cast("bool", data["needs_investigation"]),
            confidence=float(cast("float", data["confidence"])),
        )

    def to_dict(self) -> dict[str, object]:
        """Return JSON-compatible assessment data."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class IncidentAnalysis:
    """Failure classification and localization without repository mutation."""

    classification: IncidentClass
    root_cause: str
    evidence: tuple[str, ...]
    suspected_files: tuple[str, ...]
    reproduction_steps: tuple[str, ...]
    priority: Priority
    confidence: float

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> IncidentAnalysis:
        """Build an incident analysis from schema-validated data."""
        return cls(
            classification=cast("IncidentClass", data["classification"]),
            root_cause=cast("str", data["root_cause"]),
            evidence=tuple(cast("list[str]", data["evidence"])),
            suspected_files=tuple(cast("list[str]", data["suspected_files"])),
            reproduction_steps=tuple(cast("list[str]", data["reproduction_steps"])),
            priority=cast("Priority", data["priority"]),
            confidence=float(cast("float", data["confidence"])),
        )

    def to_dict(self) -> dict[str, object]:
        """Return JSON-compatible incident data."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    """Combined outputs and call identities from the diagnostic pipeline."""

    assessment: RunAssessment
    assessment_call_id: str
    incident: IncidentAnalysis | None = None
    incident_call_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a portable pipeline result."""
        return {
            "assessment": self.assessment.to_dict(),
            "assessment_call_id": self.assessment_call_id,
            "incident": self.incident.to_dict() if self.incident else None,
            "incident_call_id": self.incident_call_id,
        }


class RunCriticAgent:
    """Interpret runtime and goal facts without tools or write access."""

    def __init__(self, client: StructuredLLM) -> None:
        """Retain only the structured completion capability."""
        self._client = client

    def analyze(
        self,
        context: dict[str, object],
        *,
        budget: LLMBudget,
    ) -> tuple[RunAssessment, str]:
        """Return a bounded assessment and provider call ID."""
        completion = self._client.complete_structured(
            task="run_critic",
            messages=(
                ChatMessage(
                    "system",
                    "You are a read-only run critic. Use only supplied facts, cite concrete "
                    "evidence, do not invent files, and do not propose code patches.",
                ),
                ChatMessage("user", json.dumps(context, sort_keys=True)),
            ),
            schema_name="run_assessment",
            schema=_RUN_ASSESSMENT_SCHEMA,
            budget=budget,
            max_output_tokens=1_200,
            max_attempts=2,
        )
        return RunAssessment.from_dict(completion.data), completion.call_id


class IncidentAnalystAgent:
    """Classify and localize incidents without tools or repository access."""

    def __init__(self, client: StructuredLLM) -> None:
        """Retain only the structured completion capability."""
        self._client = client

    def analyze(
        self,
        context: dict[str, object],
        assessment: RunAssessment,
        *,
        budget: LLMBudget,
    ) -> tuple[IncidentAnalysis, str]:
        """Return a bounded incident report and provider call ID."""
        payload = {"run": context, "assessment": assessment.to_dict()}
        completion = self._client.complete_structured(
            task="incident_analyst",
            messages=(
                ChatMessage(
                    "system",
                    "You are a read-only incident analyst. Classify the incident using only "
                    "supplied evidence. Do not execute commands, write files, or produce a patch.",
                ),
                ChatMessage("user", json.dumps(payload, sort_keys=True)),
            ),
            schema_name="incident_analysis",
            schema=_INCIDENT_SCHEMA,
            budget=budget,
            max_output_tokens=1_200,
            max_attempts=2,
        )
        return IncidentAnalysis.from_dict(completion.data), completion.call_id


class DiagnosticPipeline:
    """Coordinate read-only agents under one shared budget."""

    def __init__(
        self,
        *,
        client: StructuredLLM,
        store: SQLiteDiagnosticStore | None = None,
    ) -> None:
        """Create critic and incident roles over the same bounded client."""
        self._critic = RunCriticAgent(client)
        self._incident_analyst = IncidentAnalystAgent(client)
        self._store = store

    def analyze(self, run: OrchestratedRun, *, budget: LLMBudget) -> DiagnosticResult:
        """Assess a run and investigate only when deterministic facts justify it."""
        context = build_diagnostic_context(run)
        assessment, assessment_call_id = self._critic.analyze(context, budget=budget)
        incident: IncidentAnalysis | None = None
        incident_call_id: str | None = None
        if _needs_incident(run, assessment):
            incident, incident_call_id = self._incident_analyst.analyze(
                context,
                assessment,
                budget=budget,
            )
        result = DiagnosticResult(
            assessment=assessment,
            assessment_call_id=assessment_call_id,
            incident=incident,
            incident_call_id=incident_call_id,
        )
        if self._store:
            self._store.save(run.run_id, result)
        return result


def build_diagnostic_context(
    run: OrchestratedRun,
    *,
    max_log_lines: int = _DEFAULT_MAX_LOG_LINES,
    max_line_chars: int = _DEFAULT_MAX_LINE_CHARS,
) -> dict[str, object]:
    """Create a bounded, redacted, source-free context from observed facts."""
    report = run.report
    return {
        "run_id": run.run_id,
        "runtime": {
            "command": [_redact(item, max_line_chars) for item in report.command],
            "outcome": report.outcome.value,
            "exit_code": report.exit_code,
            "timed_out": report.timed_out,
            "duration_seconds": report.duration_seconds,
            "peak_rss_bytes": report.peak_rss_bytes,
            "stdout": _bounded_lines(report.stdout, max_log_lines, max_line_chars),
            "stderr": _bounded_lines(report.stderr, max_log_lines, max_line_chars),
            "artifacts": {
                "created": [artifact.path for artifact in report.artifacts.created],
                "modified": [artifact.path for artifact in report.artifacts.modified],
                "deleted": [artifact.path for artifact in report.artifacts.deleted],
            },
        },
        "goal": (
            run.goal_evaluation.to_dict()
            if run.goal_evaluation
            else {"overall": "unavailable", "checks": []}
        ),
        "repository": {
            "commit": run.repository_facts.commit,
            "branch": run.repository_facts.branch,
            "dirty": run.repository_facts.dirty,
        },
        "static_analysis": {
            "syntax_errors": [asdict(item) for item in run.analysis.syntax_errors[:20]],
            "tools": [
                {"name": tool.name, "status": tool.status, "exit_code": tool.exit_code}
                for tool in run.analysis.tools
            ],
            "symbol_count": len(run.analysis.symbols),
        },
    }


_DIAGNOSTIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_diagnostics (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    assessment_call_id TEXT NOT NULL,
    assessment_json TEXT NOT NULL,
    incident_call_id TEXT,
    incident_json TEXT
);
"""


class SQLiteDiagnosticStore:
    """Persist structured diagnostics against an existing run database."""

    def __init__(self, database: str | Path) -> None:
        """Initialize the diagnostics projection."""
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_DIAGNOSTIC_SCHEMA)

    def save(self, run_id: str, result: DiagnosticResult) -> None:
        """Persist one immutable pipeline result."""
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO run_diagnostics (
                    run_id, assessment_call_id, assessment_json,
                    incident_call_id, incident_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.assessment_call_id,
                    json.dumps(result.assessment.to_dict(), sort_keys=True),
                    result.incident_call_id,
                    json.dumps(result.incident.to_dict(), sort_keys=True)
                    if result.incident
                    else None,
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a foreign-key-enforcing connection."""
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _needs_incident(run: OrchestratedRun, assessment: RunAssessment) -> bool:
    """Combine deterministic terminal facts with critic escalation."""
    goal_failed = run.goal_evaluation is not None and run.goal_evaluation.overall == "failed"
    return (
        report_failed(run)
        or goal_failed
        or assessment.needs_investigation
        or any(finding.severity in {"high", "critical"} for finding in assessment.findings)
    )


def report_failed(run: OrchestratedRun) -> bool:
    """Return whether the process terminal outcome is not successful."""
    return run.report.outcome is not RunOutcome.SUCCEEDED


def _bounded_lines(
    lines: tuple[CapturedLine, ...],
    maximum: int,
    max_chars: int,
) -> list[str]:
    """Return redacted message text under explicit count and size bounds."""
    return [_redact(line.message, max_chars) for line in lines[:maximum]]


def _redact(text: str, max_chars: int) -> str:
    """Remove common credential forms and cap serialized text size."""
    redacted = _SECRET_TOKEN.sub("[REDACTED]", text)
    redacted = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", redacted)
    return redacted[:max_chars]


_FINDING_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["correctness", "performance", "reliability", "quality"],
        },
        "severity": {
            "type": "string",
            "enum": ["info", "low", "medium", "high", "critical"],
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": 500},
        "evidence": {
            "type": "array",
            "items": {"type": "string", "maxLength": 500},
            "maxItems": 10,
        },
        "recommendation": {"type": "string", "minLength": 1, "maxLength": 500},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "category",
        "severity",
        "summary",
        "evidence",
        "recommendation",
        "confidence",
    ],
    "additionalProperties": False,
}

_RUN_ASSESSMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "goal_alignment": {
            "type": "string",
            "enum": ["satisfied", "violated", "unknown"],
        },
        "findings": {"type": "array", "items": _FINDING_SCHEMA, "maxItems": 12},
        "needs_investigation": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "summary",
        "goal_alignment",
        "findings",
        "needs_investigation",
        "confidence",
    ],
    "additionalProperties": False,
}

_INCIDENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": [
                "runtime_error",
                "timeout",
                "resource",
                "goal_violation",
                "quality",
                "unknown",
            ],
        },
        "root_cause": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "evidence": {
            "type": "array",
            "items": {"type": "string", "maxLength": 500},
            "maxItems": 12,
        },
        "suspected_files": {
            "type": "array",
            "items": {"type": "string", "maxLength": 300},
            "maxItems": 12,
        },
        "reproduction_steps": {
            "type": "array",
            "items": {"type": "string", "maxLength": 500},
            "maxItems": 10,
        },
        "priority": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "classification",
        "root_cause",
        "evidence",
        "suspected_files",
        "reproduction_steps",
        "priority",
        "confidence",
    ],
    "additionalProperties": False,
}

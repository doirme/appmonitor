"""Bounded decisions and counters for post-repair local restarts."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, cast

from appmonitor.openrouter import ChatMessage

if TYPE_CHECKING:
    from appmonitor.agents import DiagnosticResult, StructuredLLM
    from appmonitor.openrouter import LLMBudget
    from appmonitor.patching import PatchPipelineResult

RecoveryAction = Literal["restart", "stop"]


class RecoveryLimitError(RuntimeError):
    """Raised when a configured restart boundary is exhausted."""


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    """One auditable recommendation after a verified patch."""

    action: RecoveryAction
    reason: str
    confidence: float
    call_id: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous decisions."""
        if not self.reason.strip():
            message = "recovery decision reason must not be empty"
            raise ValueError(message)
        if not 0 <= self.confidence <= 1:
            message = "recovery decision confidence must be between 0 and 1"
            raise ValueError(message)

    def to_dict(self) -> dict[str, object]:
        """Return a portable decision record."""
        return {
            "action": self.action,
            "reason": self.reason,
            "confidence": self.confidence,
            "call_id": self.call_id,
            "model": self.model,
        }


class RecoveryDecisionMaker(Protocol):
    """Choose whether a verified patch should be restarted."""

    def decide(
        self,
        diagnostic: DiagnosticResult,
        patch: PatchPipelineResult,
        *,
        budget: LLMBudget,
    ) -> RecoveryDecision:
        """Return a bounded restart or stop recommendation."""


class RecoveryDecisionAgent:
    """Ask an untrusted structured model for a post-patch recommendation."""

    def __init__(self, client: StructuredLLM) -> None:
        """Retain only the structured completion capability."""
        self._client = client

    def decide(
        self,
        diagnostic: DiagnosticResult,
        patch: PatchPipelineResult,
        *,
        budget: LLMBudget,
    ) -> RecoveryDecision:
        """Recommend restart or stop from bounded diagnostic and validation facts."""
        completion = self._client.complete_structured(
            task="recovery_decision",
            messages=(
                ChatMessage(
                    "system",
                    "Decide whether to restart a locally verified repair. Choose stop when the "
                    "critical defect remains unsafe, validation evidence is insufficient, or "
                    "continued execution risks data or external side effects.",
                ),
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "diagnostic": diagnostic.to_dict(),
                            "patch": patch.to_dict(),
                        },
                        sort_keys=True,
                    ),
                ),
            ),
            schema_name="recovery_decision",
            schema=_RECOVERY_DECISION_SCHEMA,
            budget=budget,
            max_output_tokens=500,
            max_attempts=2,
        )
        return RecoveryDecision(
            action=cast("RecoveryAction", completion.data["action"]),
            reason=cast("str", completion.data["reason"]),
            confidence=float(cast("float", completion.data["confidence"])),
            call_id=completion.call_id,
            model=completion.model,
        )


@dataclass(slots=True)
class RecoveryLimits:
    """Mutable restart count and elapsed-time limits shared across repair cycles."""

    max_restarts: int | None = 3
    max_duration_seconds: float | None = 30 * 60
    restarts: int = field(default=0, init=False)
    _started: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Reject zero or negative finite bounds."""
        if self.max_restarts is not None and self.max_restarts <= 0:
            message = "maximum recovery restarts must be greater than zero or None"
            raise ValueError(message)
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            message = "maximum recovery duration must be greater than zero or None"
            raise ValueError(message)

    def begin_restart(self) -> None:
        """Consume one restart after checking count and elapsed-time limits."""
        if self.max_restarts is not None and self.restarts >= self.max_restarts:
            message = f"recovery restart limit reached ({self.max_restarts})"
            raise RecoveryLimitError(message)
        now = time.monotonic()
        if self._started is None:
            self._started = now
        elapsed = now - self._started
        if self.max_duration_seconds is not None and elapsed >= self.max_duration_seconds:
            message = f"recovery duration limit reached ({self.max_duration_seconds:g} seconds)"
            raise RecoveryLimitError(message)
        self.restarts += 1


_RECOVERY_DECISION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["restart", "stop"]},
        "reason": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["action", "reason", "confidence"],
    "additionalProperties": False,
}

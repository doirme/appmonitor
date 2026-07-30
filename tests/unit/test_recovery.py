"""Tests for controlled post-repair restart decisions and limits."""

from __future__ import annotations

import pytest

from appmonitor import LLMBudget
from appmonitor.agents import DiagnosticResult, RunAssessment
from appmonitor.openrouter import (
    ChatMessage,
    LLMUsage,
    ModelRoutingConstraints,
    StructuredCompletion,
)
from appmonitor.patching import PatchPipelineResult, PatchPlan, PatchValidation
from appmonitor.recovery import (
    RecoveryDecisionAgent,
    RecoveryLimitError,
    RecoveryLimits,
)

_UNLIMITED_RESTARTS_EXERCISED = 10


class FakeStructuredClient:
    """Return one fixed restart decision."""

    def complete_structured(  # noqa: PLR0913 - production protocol fixture
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
        routing: ModelRoutingConstraints | None = None,
    ) -> StructuredCompletion:
        """Return a schema-shaped stop order."""
        del messages, schema_name, schema, min_context_tokens, max_output_tokens, max_attempts
        del routing
        assert task == "recovery_decision"
        budget.begin_call(0)
        budget.finish_call(0, 0)
        return StructuredCompletion(
            call_id="recovery-call",
            model="reviewer/model",
            data={
                "action": "stop",
                "reason": "validated patch does not address the critical data risk",
                "confidence": 0.95,
            },
            usage=LLMUsage(10, 5, 0),
            latency_seconds=0.01,
        )


def test_recovery_agent_can_order_a_complete_stop() -> None:
    """The LLM emits a bounded recommendation rather than controlling processes directly."""
    decision = RecoveryDecisionAgent(FakeStructuredClient()).decide(
        _diagnostic(),
        _patch(),
        budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    )

    assert decision.action == "stop"
    assert decision.call_id == "recovery-call"
    assert decision.model == "reviewer/model"


def test_recovery_limits_accept_none_and_bound_defaults() -> None:
    """Users may explicitly remove restart and duration limits."""
    unlimited = RecoveryLimits(max_restarts=None, max_duration_seconds=None)

    for _ in range(_UNLIMITED_RESTARTS_EXERCISED):
        unlimited.begin_restart()

    assert unlimited.restarts == _UNLIMITED_RESTARTS_EXERCISED


def test_recovery_limits_reject_restart_after_configured_count() -> None:
    """A default-style bounded policy stops repeated repair cycles."""
    limits = RecoveryLimits(max_restarts=1, max_duration_seconds=60)
    limits.begin_restart()

    with pytest.raises(RecoveryLimitError, match="restart limit"):
        limits.begin_restart()


def test_recovery_limits_reject_invalid_values() -> None:
    """Zero and negative bounds are ambiguous and rejected."""
    with pytest.raises(ValueError, match="restarts"):
        RecoveryLimits(max_restarts=0)
    with pytest.raises(ValueError, match="duration"):
        RecoveryLimits(max_duration_seconds=0)


def _diagnostic() -> DiagnosticResult:
    return DiagnosticResult(
        assessment=RunAssessment(
            summary="critical failure",
            goal_alignment="violated",
            findings=(),
            needs_investigation=True,
            confidence=1.0,
        ),
        assessment_call_id="assessment",
    )


def _patch() -> PatchPipelineResult:
    return PatchPipelineResult(
        status="applied",
        reason="validated and independently approved",
        plan=PatchPlan(
            summary="repair",
            files=(),
            risk="high",
            acceptance_criteria=("tests pass",),
        ),
        patch_sha256="a" * 64,
        diff="diff",
        validation=PatchValidation(()),
        review=None,
    )

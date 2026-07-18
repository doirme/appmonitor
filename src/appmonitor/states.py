"""Deterministic run-state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class RunState(StrEnum):
    """Lifecycle states for an observed and maintained run."""

    CREATED = "created"
    REPOSITORY_PREPARED = "repository_prepared"
    ANALYZED = "analyzed"
    ENVIRONMENT_READY = "environment_ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    REVIEWED = "reviewed"
    PATCH_PLANNED = "patch_planned"
    PATCH_APPLIED = "patch_applied"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    REJECTED = "rejected"
    COMMITTED = "committed"
    PUSHED = "pushed"
    REPORTED = "reported"


class InvalidTransitionError(RuntimeError):
    """Raised when a requested state transition is not in the policy graph."""


@dataclass(frozen=True, slots=True)
class StateTransition:
    """Auditable record of one accepted state transition."""

    previous: RunState
    current: RunState
    cause: str
    actor: str
    timestamp: datetime


_RUN_TERMINALS = {
    RunState.SUCCEEDED,
    RunState.FAILED,
    RunState.TIMED_OUT,
    RunState.RESOURCE_LIMIT_EXCEEDED,
}

_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.REPOSITORY_PREPARED}),
    RunState.REPOSITORY_PREPARED: frozenset({RunState.ANALYZED}),
    RunState.ANALYZED: frozenset({RunState.ENVIRONMENT_READY}),
    RunState.ENVIRONMENT_READY: frozenset({RunState.RUNNING}),
    RunState.RUNNING: frozenset(_RUN_TERMINALS),
    **{state: frozenset({RunState.REVIEWED}) for state in _RUN_TERMINALS},
    RunState.REVIEWED: frozenset({RunState.PATCH_PLANNED, RunState.REPORTED}),
    RunState.PATCH_PLANNED: frozenset({RunState.PATCH_APPLIED, RunState.REJECTED}),
    RunState.PATCH_APPLIED: frozenset({RunState.VERIFYING}),
    RunState.VERIFYING: frozenset({RunState.VERIFIED, RunState.REJECTED}),
    RunState.VERIFIED: frozenset({RunState.COMMITTED, RunState.REPORTED}),
    RunState.REJECTED: frozenset({RunState.REPORTED}),
    RunState.COMMITTED: frozenset({RunState.PUSHED, RunState.REPORTED}),
    RunState.PUSHED: frozenset({RunState.REPORTED}),
    RunState.REPORTED: frozenset(),
}


class RunStateMachine:
    """Enforce and record the run lifecycle graph."""

    def __init__(self) -> None:
        """Create a state machine at the initial state."""
        self._state = RunState.CREATED
        self._history: list[StateTransition] = []

    @property
    def state(self) -> RunState:
        """Return the current state."""
        return self._state

    @property
    def history(self) -> tuple[StateTransition, ...]:
        """Return immutable transition history."""
        return tuple(self._history)

    def transition(self, target: RunState, *, cause: str, actor: str) -> StateTransition:
        """Apply an authorized transition and append its audit record."""
        if target not in _ALLOWED_TRANSITIONS[self._state]:
            msg = f"invalid transition: {self._state.name} -> {target.name}"
            raise InvalidTransitionError(msg)
        record = StateTransition(
            previous=self._state,
            current=target,
            cause=cause,
            actor=actor,
            timestamp=datetime.now(UTC),
        )
        self._state = target
        self._history.append(record)
        return record


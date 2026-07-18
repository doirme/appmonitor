"""Tests for deterministic run-state transitions."""

import pytest

from appmonitor.states import InvalidTransitionError, RunState, RunStateMachine


def test_state_machine_records_authorized_transition() -> None:
    """An authorized transition changes state and creates an audit record."""
    machine = RunStateMachine()

    transition = machine.transition(
        RunState.REPOSITORY_PREPARED,
        cause="repository validated",
        actor="system",
    )

    assert machine.state is RunState.REPOSITORY_PREPARED
    assert transition.previous is RunState.CREATED
    assert transition.current is RunState.REPOSITORY_PREPARED
    assert transition.cause == "repository validated"
    assert transition.timestamp.tzinfo is not None


def test_state_machine_rejects_unauthorized_transition() -> None:
    """State changes must follow the declared graph."""
    machine = RunStateMachine()

    with pytest.raises(InvalidTransitionError, match=r"CREATED.*RUNNING"):
        machine.transition(RunState.RUNNING, cause="skip setup", actor="model")

    assert machine.state is RunState.CREATED
    assert machine.history == ()

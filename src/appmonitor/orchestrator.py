"""Deterministic orchestration of one monitored run."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from appmonitor.execution import LocalExecutor, RunOutcome
from appmonitor.persistence import SQLiteRunStore
from appmonitor.states import RunState, RunStateMachine

if TYPE_CHECKING:
    from appmonitor.execution import RunReport
    from appmonitor.models import RunSpec
    from appmonitor.states import StateTransition

_OUTCOME_STATES = {
    RunOutcome.SUCCEEDED: RunState.SUCCEEDED,
    RunOutcome.FAILED: RunState.FAILED,
    RunOutcome.TIMED_OUT: RunState.TIMED_OUT,
}


@dataclass(frozen=True, slots=True)
class OrchestratedRun:
    """Result of a fully orchestrated and persisted local run."""

    run_id: str
    report: RunReport
    transitions: tuple[StateTransition, ...]

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize the report together with its identity and lifecycle."""
        payload = json.loads(self.report.to_json())
        payload["run_id"] = self.run_id
        payload["transitions"] = [
            {
                "previous": transition.previous.value,
                "current": transition.current.value,
                "cause": transition.cause,
                "actor": transition.actor,
                "timestamp": transition.timestamp.isoformat(),
            }
            for transition in self.transitions
        ]
        return json.dumps(payload, indent=indent, sort_keys=True)


class RunClient:
    """Coordinate deterministic execution, lifecycle tracking, and persistence."""

    def __init__(
        self,
        *,
        executor: LocalExecutor | None = None,
        store: SQLiteRunStore | None = None,
    ) -> None:
        """Create a client with optional injected infrastructure."""
        self._executor = executor or LocalExecutor()
        self._store = store

    def execute(self, spec: RunSpec) -> OrchestratedRun:
        """Execute, classify, and atomically persist a monitored run."""
        machine = RunStateMachine()
        machine.transition(
            RunState.REPOSITORY_PREPARED,
            cause="repository path validated",
            actor="system",
        )
        machine.transition(
            RunState.ANALYZED,
            cause="run specification validated",
            actor="system",
        )
        machine.transition(
            RunState.ENVIRONMENT_READY,
            cause="local process environment selected",
            actor="system",
        )
        machine.transition(
            RunState.RUNNING,
            cause="target process started",
            actor="system",
        )
        report = self._executor.execute(spec)
        machine.transition(
            _OUTCOME_STATES[report.outcome],
            cause=f"target process outcome: {report.outcome.value}",
            actor="system",
        )
        machine.transition(
            RunState.REVIEWED,
            cause="deterministic execution facts collected",
            actor="system",
        )
        machine.transition(
            RunState.REPORTED,
            cause="portable run report generated",
            actor="system",
        )

        run_id = str(uuid4())
        store = self._store or SQLiteRunStore(
            spec.repository / ".appmonitor" / "runs.sqlite3",
        )
        store.save(report, run_id=run_id, transitions=machine.history)
        return OrchestratedRun(run_id=run_id, report=report, transitions=machine.history)


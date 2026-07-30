"""Deterministic orchestration of one monitored run."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from appmonitor.analysis import StaticAnalysisReport, StaticAnalyzer
from appmonitor.execution import LocalExecutor, RunOutcome
from appmonitor.git_workflow import GitRemotePublisher, RemoteGitPreflight
from appmonitor.goal import GoalContract, GoalEvaluation, GoalEvaluator, load_goal_contract
from appmonitor.persistence import SQLiteRunStore
from appmonitor.repository import (
    EnvironmentFacts,
    EnvironmentPreparationError,
    EnvironmentPreparer,
    RepositoryInspector,
)
from appmonitor.states import RunState, RunStateMachine

if TYPE_CHECKING:
    from appmonitor.execution import RunReport
    from appmonitor.models import RunSpec
    from appmonitor.repository import RepositoryFacts
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
    repository_facts: RepositoryFacts
    environment_facts: EnvironmentFacts
    analysis: StaticAnalysisReport
    goal_contract: GoalContract | None
    goal_evaluation: GoalEvaluation | None
    git_remote: str | None = None

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
        payload["repository_facts"] = self.repository_facts.to_dict()
        payload["environment_facts"] = self.environment_facts.to_dict()
        payload["analysis"] = self.analysis.to_dict()
        payload["goal"] = _goal_payload(self.goal_contract, self.goal_evaluation)
        payload["git_remote"] = self.git_remote
        return json.dumps(payload, indent=indent, sort_keys=True)


class RunClient:
    """Coordinate deterministic execution, lifecycle tracking, and persistence."""

    def __init__(  # noqa: PLR0913 - dependencies remain independently injectable
        self,
        *,
        executor: LocalExecutor | None = None,
        store: SQLiteRunStore | None = None,
        repository_inspector: RepositoryInspector | None = None,
        environment_preparer: EnvironmentPreparer | None = None,
        static_analyzer: StaticAnalyzer | None = None,
        goal_evaluator: GoalEvaluator | None = None,
        remote_git: RemoteGitPreflight | None = None,
    ) -> None:
        """Create a client with optional injected infrastructure."""
        self._executor = executor or LocalExecutor()
        self._store = store
        self._repository_inspector = repository_inspector or RepositoryInspector()
        self._environment_preparer = environment_preparer or EnvironmentPreparer()
        self._static_analyzer = static_analyzer or StaticAnalyzer()
        self._goal_evaluator = goal_evaluator or GoalEvaluator()
        self._remote_git = remote_git or GitRemotePublisher()

    def execute(self, spec: RunSpec) -> OrchestratedRun:
        """Execute, classify, and atomically persist a monitored run."""
        machine = RunStateMachine()
        run_id = str(uuid4())
        goal_contract = load_goal_contract(spec.goal_file) if spec.goal_file else None
        repository_facts = self._repository_inspector.inspect(spec.repository)
        machine.transition(
            RunState.REPOSITORY_PREPARED,
            cause=_repository_cause(repository_facts),
            actor="system",
        )
        if spec.git_remote:
            self._remote_git.preflight(
                spec.repository,
                run_id=run_id,
                remote=spec.git_remote,
            )
        analysis = (
            self._static_analyzer.analyze(spec.repository)
            if spec.analyze_repository
            else StaticAnalysisReport()
        )
        machine.transition(
            RunState.ANALYZED,
            cause=_analysis_cause(analysis, requested=spec.analyze_repository),
            actor="system",
        )
        environment_facts = EnvironmentFacts.current()
        if spec.sync_environment:
            environment_facts = self._environment_preparer.prepare(spec.repository)
            if not environment_facts.uv_sync_succeeded:
                msg = (
                    "uv sync --frozen failed with exit code "
                    f"{environment_facts.uv_sync_exit_code}: {environment_facts.uv_sync_stderr}"
                )
                raise EnvironmentPreparationError(msg)
        machine.transition(
            RunState.ENVIRONMENT_READY,
            cause=_environment_cause(environment_facts),
            actor="system",
        )
        machine.transition(
            RunState.RUNNING,
            cause="target process started",
            actor="system",
        )
        report = self._executor.execute(spec)
        goal_evaluation = (
            self._goal_evaluator.evaluate(goal_contract, report) if goal_contract else None
        )
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

        store = self._store or SQLiteRunStore(
            spec.repository / ".appmonitor" / "runs.sqlite3",
        )
        store.save(
            report,
            run_id=run_id,
            transitions=machine.history,
            repository_facts=repository_facts,
            environment_facts=environment_facts,
            analysis=analysis,
            goal_contract=goal_contract,
            goal_evaluation=goal_evaluation,
        )
        return OrchestratedRun(
            run_id=run_id,
            report=report,
            transitions=machine.history,
            repository_facts=repository_facts,
            environment_facts=environment_facts,
            analysis=analysis,
            goal_contract=goal_contract,
            goal_evaluation=goal_evaluation,
            git_remote=spec.git_remote,
        )


def _repository_cause(facts: RepositoryFacts) -> str:
    """Describe the repository preparation result for lifecycle audit."""
    if not facts.is_git_repository:
        return "local non-Git repository path validated"
    return f"Git repository prepared at commit {facts.commit or 'unknown'}"


def _environment_cause(facts: EnvironmentFacts) -> str:
    """Describe whether uv preparation was requested and completed."""
    if facts.uv_sync_performed:
        return "uv sync --frozen completed"
    return "current local process environment selected"


def _analysis_cause(report: StaticAnalysisReport, *, requested: bool) -> str:
    """Describe static-analysis work performed for lifecycle audit."""
    if not requested:
        return "run specification validated; static analysis not requested"
    return (
        f"static analysis indexed {len(report.symbols)} symbols and "
        f"ran {len(report.tools)} quality tools"
    )


def _goal_payload(
    contract: GoalContract | None,
    evaluation: GoalEvaluation | None,
) -> dict[str, object] | None:
    """Build the portable goal section."""
    if contract is None or evaluation is None:
        return None
    return {"contract": contract.to_dict(), "evaluation": evaluation.to_dict()}

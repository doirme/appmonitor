# Orchestration, repositories, and state

## `RunClient`

```python
RunClient(
    *,
    executor: LocalExecutor | None = None,
    store: SQLiteRunStore | None = None,
    repository_inspector: RepositoryInspector | None = None,
    environment_preparer: EnvironmentPreparer | None = None,
    static_analyzer: StaticAnalyzer | None = None,
    goal_evaluator: GoalEvaluator | None = None,
)
```

`execute(spec: RunSpec) -> OrchestratedRun` inspects the repository, optionally analyzes it,
optionally executes `uv sync --frozen`, runs the process, evaluates its goal, advances the state
machine, and persists the evidence. Without an injected store it writes
`<repository>/.appmonitor/runs.sqlite3`.

Dependency injection is intended for tests and alternate infrastructure. Environment preparation
failure raises `EnvironmentPreparationError` before the monitored process starts.

## `OrchestratedRun`

| Field | Meaning |
| --- | --- |
| `run_id` | Durable UUID string |
| `report` | `RunReport` from the executor |
| `transitions` | Ordered immutable state history |
| `repository_facts` | Git revision and project identity |
| `environment_facts` | Interpreter and uv preparation result |
| `analysis` | Static analysis report, empty when not requested |
| `goal_contract` | Loaded goal or `None` |
| `goal_evaluation` | Evaluated checks or `None` |

`to_json(indent=2)` combines all these projections into one portable document.

## Repository facts

`RepositoryFacts` contains `root`, `is_git_repository`, `commit`, `branch`, `dirty`,
`has_pyproject`, `has_uv_lock`, and `uv_lock_sha256`. `RepositoryInspector.inspect(path)` is
read-only. A non-Git directory is valid and returns nullable Git fields.

`EnvironmentFacts.current()` records the active Python executable and version. When
`EnvironmentPreparer.prepare(path)` runs, it executes exactly `uv sync --frozen` through an
argument vector and records the exit code and captured streams.

`RepositoryInspector` and `EnvironmentPreparer` are supporting module APIs in
`appmonitor.repository`; their result models are exported from the package root.

## State machine

The supporting state API is imported from `appmonitor.states`:

```python
from appmonitor.states import RunState, RunStateMachine

machine = RunStateMachine()
transition = machine.transition(
    RunState.REPOSITORY_PREPARED,
    cause="repository validated",
    actor="system",
)
```

Normal orchestration follows:

```text
created -> repository_prepared -> analyzed -> environment_ready -> running
        -> succeeded | failed | timed_out -> reviewed -> reported
```

`transition()` returns `StateTransition(previous, current, cause, actor, timestamp)`. Invalid graph
edges raise `InvalidTransitionError` without changing `state` or `history`.

# API reference

## Public imports

The stable package root currently exports:

```python
from appmonitor import (
    LocalExecutor,
    OrchestratedRun,
    RunClient,
    RunOutcome,
    RunReport,
    RunSpec,
    SQLiteRunStore,
)
```

Other modules are implementation-level APIs until explicitly documented here.

## `RunSpec`

```python
RunSpec(
    repository: str | Path,
    command: Sequence[str],
    timeout_seconds: float | None = None,
    base_branch: str | None = None,
    goal_file: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
)
```

Immutable description of one execution. `repository` is resolved to an absolute existing
directory, `command` becomes a tuple, and `environment` is copied into a read-only mapping.
An empty command, missing repository, or non-positive timeout raises `ValueError`.

The environment mapping overlays the current process environment. Callers must avoid passing
secrets unless the monitored program explicitly requires them.

## `LocalExecutor`

```python
report = LocalExecutor().execute(
    RunSpec(
        repository="./project",
        command=("uv", "run", "python", "main.py"),
        timeout_seconds=300,
    )
)
```

`execute(spec)` runs the command in the repository and always returns a `RunReport` for normal
completion, non-zero exit, or timeout. Startup errors such as an unknown executable are raised
to the caller. Output is decoded as UTF-8 with replacement for invalid bytes.

The executor captures stdout and stderr independently, samples aggregate process-tree metrics,
terminates descendants on timeout, and compares repository snapshots around the run.

`LocalExecutor` is the low-level API. It does not persist a run or advance lifecycle states. Use
`RunClient` for the normal complete workflow.

## `RunClient`

```python
from appmonitor import RunClient, RunSpec

result = RunClient().execute(
    RunSpec(repository="./project", command=("python", "main.py")),
)
```

`execute(spec)` validates and advances the deterministic lifecycle, delegates process observation
to `LocalExecutor`, maps process facts to a terminal state, and atomically persists the report and
transition history.

Without an injected store, the database is `<repository>/.appmonitor/runs.sqlite3`. Tests and
applications may inject `RunClient(store=SQLiteRunStore(path))`. An executor can also be injected
for controlled infrastructure testing.

The returned `OrchestratedRun` contains:

- `run_id`: durable UUID string;
- `report`: the underlying `RunReport`;
- `transitions`: immutable ordered lifecycle records;
- `to_json(indent=2)`: report JSON enriched with `run_id` and transitions.

## `RunReport`

Important fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `command` | `tuple[str, ...]` | Exact requested argument vector |
| `repository` | `str` | Absolute working directory |
| `outcome` | `RunOutcome` | `succeeded`, `failed`, or `timed_out` |
| `exit_code` | `int | None` | Child exit status when available |
| `timed_out` | `bool` | Whether the runtime budget was exceeded |
| `started_at`, `finished_at` | `datetime` | UTC execution bounds |
| `stdout`, `stderr` | `tuple[CapturedLine, ...]` | Timestamped output lines |
| `metrics` | `tuple[ProcessMetrics, ...]` | Process-tree samples |
| `artifacts` | `ArtifactChanges` | Created, modified, and deleted files |

Computed properties:

- `duration_seconds`: wall-clock duration.
- `peak_rss_bytes`: largest observed aggregate resident memory.
- `to_json(indent=2)`: stable, sorted JSON representation suitable for storage.

Each `ProcessMetrics` sample includes UTC timestamp, aggregate RSS, aggregate CPU percentage,
observed process count, and thread count. Each artifact includes its relative POSIX path, size,
nanosecond modification time, and SHA-256 digest.

## State machine

The lifecycle API is currently imported from `appmonitor.states`:

```python
from appmonitor.states import RunState, RunStateMachine

machine = RunStateMachine()
machine.transition(
    RunState.REPOSITORY_PREPARED,
    cause="repository validated",
    actor="system",
)
```

`transition()` returns an immutable `StateTransition` containing previous/current states,
cause, actor, and UTC timestamp. A transition outside the declared graph raises
`InvalidTransitionError` and leaves state and history unchanged. `history` is exposed as an
immutable tuple.

## `SQLiteRunStore`

```python
from appmonitor import SQLiteRunStore

store = SQLiteRunStore(".appmonitor/runs.sqlite3")
run_id = store.save(report)
stored = store.load(run_id)
```

The store initializes its schema automatically. `save(report, run_id=None)` writes the run,
ordered stdout/stderr lines, metric samples, and classified artifacts in one transaction. The
generated identifier is a UUID string; callers may provide a stable identifier for integration
with an orchestrator. Duplicate identifiers raise `sqlite3.IntegrityError` with no partial child
rows committed.

`load(run_id)` returns the JSON-compatible `StoredRun` mapping. An unknown identifier raises
`KeyError`. Every connection enables SQLite foreign-key enforcement, and deleting a run cascades
to its normalized logs, metrics, and artifacts.

Current tables:

- `runs`: command, repository, outcome, timing, and complete portable JSON report;
- `log_lines`: stream, sequence, timestamp, and message;
- `metrics`: ordered RSS, CPU, process, and thread samples;
- `artifacts`: change class, path, size, modification time, and SHA-256 digest.
- `run_states`: ordered previous/current states, cause, actor, and timestamp.

## CLI

```bash
appmonitor run --repo ./project --timeout 300 -- uv run python main.py
```

The command writes one enriched JSON report to stdout and persists the run in
`<repository>/.appmonitor/runs.sqlite3`. The output includes `run_id` and `transitions`. The `--`
separator is optional but advised when the monitored command contains its own options.

## Planned API

The following names from the initial plan are not implemented yet and are therefore not public:

- `monitored()` for in-process instrumentation;
- `OutputArtifact` and `ResourceBudget` goal-contract models.

# API reference

## Public imports

The stable package root currently exports:

```python
from appmonitor import (
    GoalContract,
    GoalContractError,
    GoalEvaluation,
    GoalEvaluator,
    LocalExecutor,
    OrchestratedRun,
    RunClient,
    RunOutcome,
    RunReport,
    RunSpec,
    SQLiteRunStore,
    StaticAnalysisReport,
    StaticAnalyzer,
    load_goal_contract,
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
    *,
    sync_environment: bool = False,
    analyze_repository: bool = False,
)
```

Immutable description of one execution. `repository` is resolved to an absolute existing
directory, `command` becomes a tuple, and `environment` is copied into a read-only mapping.
An empty command, missing repository, or non-positive timeout raises `ValueError`.

The environment mapping overlays the current process environment. Callers must avoid passing
secrets unless the monitored program explicitly requires them.

`sync_environment=True` explicitly requests `uv sync --frozen` before target execution. A failed
sync raises `EnvironmentPreparationError` and prevents the target command from starting. The
option is keyword-only to make this environment-changing choice visible at call sites.

`analyze_repository=True` runs deterministic AST and quality-tool analysis before the monitored
target. It is opt-in because collection and coverage can execute a project's test suite.

`goal_file` selects a version-one YAML contract. `RunClient` loads it before execution, evaluates
it after observation, and persists both its SHA-256 and result.

## Goal contracts

```python
from pathlib import Path

from appmonitor import GoalEvaluator, load_goal_contract

contract = load_goal_contract(Path("goal.yaml"))
evaluation = GoalEvaluator().evaluate(contract, report)
```

`load_goal_contract()` uses PyYAML safe loading and a closed schema. Unknown sections or fields,
unsupported versions, invalid types, and malformed YAML raise `GoalContractError`. Contracts
cannot contain executable expressions.

`GoalEvaluation.overall` is `passed`, `partial`, or `failed`. Its immutable `checks` distinguish
`passed`, `failed`, and `unavailable` observations. See the
[goal contract tutorial](../tutorials/goal-contract.md) for the complete schema.

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
- `repository_facts`: Git revision, branch, dirty state, and project/lockfile identity;
- `environment_facts`: current interpreter and optional frozen uv synchronization result;
- `analysis`: AST index, syntax findings, and deterministic tool results;
- `goal_contract`: normalized goal and source SHA-256, or `None`;
- `goal_evaluation`: deterministic checks and aggregate result, or `None`;
- `to_json(indent=2)`: report JSON enriched with `run_id` and transitions.

## Repository and environment facts

`RepositoryInspector.inspect(path)` is read-only. For Git repositories it records the top-level
path, commit, current branch, and porcelain dirty state. For every directory it records whether
`pyproject.toml` and `uv.lock` exist and hashes `uv.lock` with SHA-256. A non-Git directory remains
valid and has nullable Git fields.

`EnvironmentPreparer.prepare(path)` executes exactly:

```text
uv sync --frozen
```

It returns `EnvironmentFacts` with the command, exit status, stdout, stderr, interpreter, and
success flag. Infrastructure commands use explicit argument vectors and working directories,
never a shell. Both components accept an injected command runner for deterministic tests.

## Static analysis

```python
from appmonitor import StaticAnalyzer

report = StaticAnalyzer(run_tools=False).analyze(repository_path)
```

The AST index reads `*.py` files without importing them. It records classes, synchronous and
asynchronous functions, qualified names, signatures, return annotations, docstrings, imports,
paths, and source lines. Syntax and UTF-8 decoding failures are retained as findings while other
files continue to be indexed.

With `run_tools=True`, the analyzer executes this immutable command set in order:

```text
uv run ruff check . --output-format json
uv run mypy .
uv run python -m compileall -q .
uv run pytest --collect-only -q
uv run pytest --cov --cov-branch --cov-report=json -q
```

Each result includes its exact argument vector, normalized status, exit code, stdout, and stderr.
Exit code 127 is `unavailable`; zero is `passed`; other codes are `failed`. Tool failure is data,
not an analyzer exception. The command list is internal and fixed, so an external caller or model
cannot inject an arbitrary analysis command.

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

Environment secret files matching `.env` or `.env.*` are excluded from artifact snapshots. They
are neither hashed into run artifacts nor persisted as file changes.

For Git repositories, snapshots follow `git ls-files --cached --others --exclude-standard`.
Tracked files and nonignored untracked files are observed; Git-ignored dependency caches and
generated workspaces are skipped. Non-Git directories retain recursive snapshot behavior.

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
- `run_contexts`: repository and environment identity JSON associated one-to-one with a run.
- `run_analyses`: complete AST and deterministic tool analysis JSON.
- `run_goals`: contract SHA-256, normalized contract JSON, and evaluation JSON.

## CLI

```bash
appmonitor run --repo ./project --timeout 300 --goal goal.yaml -- python main.py
```

The command writes one enriched JSON report to stdout and persists the run in
`<repository>/.appmonitor/runs.sqlite3`. The output includes `run_id` and `transitions`. The `--`
separator is optional but advised when the monitored command contains its own options.
`--sync-environment` is optional; without it, AppMonitor only records the current environment.
`--analyze` opts into AST indexing and the full fixed quality-tool suite.
`--goal` loads and evaluates a deterministic YAML goal contract.

## Planned API

The following names from the initial plan are not implemented yet and are therefore not public:

- `monitored()` for in-process instrumentation;
- in-process goal events emitted through a future instrumentation API.

# SQLite persistence

## `SQLiteRunStore`

```python
store = SQLiteRunStore(".appmonitor/runs.sqlite3")
run_id = store.save(report)
stored = store.load(run_id)
```

The constructor resolves the path, creates its parent directory, and initializes the schema.
`save()` accepts an optional stable `run_id` and optional orchestration projections. It writes one
transaction; duplicate IDs raise `sqlite3.IntegrityError` without partial children.

`load(run_id)` returns a JSON-compatible `StoredRun`. Unknown IDs raise `KeyError`. Older rows
receive defaults for projections added in later schema versions.

## Core tables

| Table | Content |
| --- | --- |
| `runs` | Command, outcome, timing, repository, complete report JSON |
| `log_lines` | Ordered timestamped stdout and stderr |
| `metrics` | Ordered RSS, CPU, process, and thread samples |
| `artifacts` | Created, modified, and deleted artifact metadata |
| `run_states` | Ordered lifecycle transitions |
| `run_contexts` | Repository and environment facts |
| `run_analyses` | AST and deterministic tool analysis |
| `run_goals` | Contract hash, normalized contract, and evaluation |

Foreign keys are enabled on each store connection. Child rows cascade when a run is deleted.

## Optional projections

Use one database path for correlated queries:

```python
database = repository / ".appmonitor" / "runs.sqlite3"

run_store = SQLiteRunStore(database)
telemetry = SQLiteLLMTelemetry(database)
diagnostics = SQLiteDiagnosticStore(database)
regressions = SQLiteRegressionStore(database)
patches = SQLitePatchStore(database)
```

| Table | Owner | Content |
| --- | --- | --- |
| `llm_calls` | `SQLiteLLMTelemetry` | Model, status, latency, tokens, cost, prompt hash |
| `run_diagnostics` | `SQLiteDiagnosticStore` | Assessment and optional incident |
| `run_regression_tests` | `SQLiteRegressionStore` | Proposal identity and reproduction result |
| `run_patches` | `SQLitePatchStore` | Plan, diff, validation, review, and final decision |
| `instrumented_calls` | `SQLiteInstrumentationStore` | Bounded in-process call observations |
| `run_git_maintenance` | `SQLiteGitStore` | Branch, commit, push, restart, scope, and decision |

The stores initialize only their own tables. Except for LLM calls, maintenance projections use
`run_id` foreign keys and therefore require the corresponding core run in the same database.

## Persistence and Git policy

The default database lives at `<repository>/.appmonitor/runs.sqlite3`. The entire `.appmonitor/`
directory is excluded from snapshots, worktree commit scopes, and this project's `.gitignore`.
The database should be backed up or mounted as persistent operational state, but not committed:

- SQLite is a mutable binary file that creates noisy, non-mergeable Git changes;
- logs and diagnostics may contain sensitive operational metadata;
- long-running monitoring can make it much larger than source code;
- repair branches should contain only the authorized source and regression changes.

A future export command may commit an explicitly sanitized JSON or Markdown report, never the live
database.

## Current reporting boundary

Persistence is normalized enough for SQL reporting and adaptive model scoring. The current package
has no dashboard, export command, or retention policy. Use the queries in
[Reading runtime and LLM telemetry](../tutorials/model-routing-and-observability.md) until the
planned read-only viewer is implemented.

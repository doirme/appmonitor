# Reporting and SQLite viewer

## `ReportDatabase`

`ReportDatabase` is the standard-library query boundary used by the optional Streamlit app:

```python
from appmonitor import ReportDatabase

reporting = ReportDatabase(".appmonitor/runs.sqlite3")
overview = reporting.overview()
failed = reporting.runs(outcome="failed", limit=50)
```

The constructor resolves the database path, verifies the SQLite header, and validates the complete
`runs` table contract. Missing or malformed files raise `ReportDatabaseError`. Optional tables may
be absent, as they are in databases created before the corresponding AppMonitor phase.

Every query opens a new SQLite URI connection with:

- `mode=ro`, which prevents opening the file for writes;
- `PRAGMA query_only = ON`, which rejects mutations through the connection;
- a 2,000 ms busy timeout by default, configurable with `busy_timeout_ms`.

The class never migrates, vacuums, attaches, deletes, or updates the selected database.

## Projections

| Method | Result |
| --- | --- |
| `overview()` | `OverviewStats` with run, LLM, patch, push, and recovery counters |
| `runs(...)` | Recent runs, optional exact outcome and text search |
| `run_detail(run_id)` | Complete portable report JSON |
| `runtime_metrics(run_id=None, ...)` | RSS, CPU, process, and thread samples |
| `llm_stats(...)` | Reliability, latency, tokens, and cost by task and model |
| `maintenance(...)` | Patch status joined to source-run facts |
| `git_recovery(...)` | Branch, commit, remote, push, restart, and stop facts |
| `tables()` | Present tables from the fixed AppMonitor allowlist |
| `table(name, ...)` | Bounded raw-table page |

All list methods return an immutable `ReportPage` containing `columns`, `rows`, `total`, `limit`,
and `offset`. Limits must be between 1 and 1,000; offsets cannot be negative. Dynamic values are
bound SQL parameters. Raw table identifiers are restricted to the internal allowlist.

`run_detail()` returns a `RunDetail` typed dictionary. It raises `KeyError` for an unknown run and
`ReportDatabaseError` for malformed persisted report JSON.

## `OverviewStats`

The overview separates process outcomes and operational maintenance evidence:

- runs, successes, failures, timeouts, and latest activity;
- LLM calls, total cost, structured success rate, and average latency;
- accepted and rejected patches;
- pushed branches, restart decisions, and stop decisions.

An absent optional table contributes zero counters rather than making an older database unusable.

## Viewer entry point

The `appmonitor-viewer` command is installed by the `viewer` extra. `render_app(database)` renders
the Streamlit UI and `main(argv)` launches it through Streamlit's CLI runner. Streamlit is imported
lazily, so importing `appmonitor` does not require the optional dependency.

The UI caches serializable query results for five seconds. Connections are never cached. The
Refresh button clears Streamlit's data cache.

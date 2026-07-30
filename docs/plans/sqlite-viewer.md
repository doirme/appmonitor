# Plan: lightweight SQLite viewer

## Decision

Use Streamlit for the first read-only viewer. It is the best fit for a single-user operational
tool with tabs, filters, metrics, tables, and simple charts. Datasette is lighter for generic table
browsing but would require plugins or templates for the AppMonitor-specific joins and maintenance
views. A desktop SQLite browser exposes tables but not the useful runtime and LLM projections.

Streamlit must be an optional dependency so the monitoring library remains lightweight:

```toml
[project.optional-dependencies]
viewer = ["streamlit>=..."]
```

## Database lifecycle

The viewer opens `<target-repository>/.appmonitor/runs.sqlite3` through SQLite's read-only URI mode,
enables `PRAGMA query_only`, and sets a bounded busy timeout. It never migrates, vacuums, deletes,
or edits the selected database.

The database is persistent operational state but is not committed with repair branches. Users
should persist or back up `.appmonitor/` as a local directory or mounted volume. A later export
feature may produce sanitized JSON, CSV, or Markdown snapshots suitable for Git.

## Implementation structure

1. Add `appmonitor.reporting` with tested standard-library query functions and immutable result
   records. Move the useful SQL projections from the observability tutorial into this layer.
2. Add a small `appmonitor.viewer` Streamlit entry point that only formats reporting results.
3. Accept `--database PATH`; default to `.appmonitor/runs.sqlite3`.
4. Validate the SQLite header, required tables, schema compatibility, and read-only access before
   rendering.
5. Cache query results briefly with an explicit refresh control. Do not cache database connections.

## Views

### Overview

- total runs, success/failure/timeout counts, and latest activity;
- total LLM cost, calls, structured success rate, and average latency;
- accepted/rejected patches, pushed branches, restarts, and stop decisions;
- recent incidents and budget/limit warnings.

### Runs

- filterable and paginated run table;
- outcome, command, repository, start, duration, exit code, and peak RSS;
- selected-run detail with stdout/stderr, state transitions, artifacts, goals, and static analysis.

### Runtime

- duration, peak memory, CPU, process, and thread trends;
- metric timeline for one selected run;
- failures and timeouts grouped by command and time period.

### LLM

- calls, success rate, invalid-response rate, provider errors, latency, tokens, and cost by task and
  model;
- the same `ModelTaskStats` inputs used by adaptive routing;
- failed and expensive attempts with prompt hashes but no prompt or response content.

### Maintenance

- diagnostics and incident classification;
- generated regression status;
- patch plan, changed paths, validation checks, reviewer verdict, and unified diff;
- correlations from the source run through the accepted or rejected repair.

### Git and recovery

- base commit, dedicated branch, commit, remote, and push status;
- restart/stop decision, confidence, restarted run, and resulting outcome;
- direct branch and commit identifiers for terminal inspection.

### Tables

- allow-listed raw-table browser;
- column-aware filtering, ordering, pagination, and CSV download;
- no arbitrary write SQL and no editable data grid.

## Verification

- build fixture databases through the existing store APIs;
- unit-test every aggregate and join, including empty and partially migrated databases;
- test read-only enforcement and bounded pagination;
- use Streamlit `AppTest` for tab rendering, filters, empty states, and selected-run navigation;
- run Ruff, mypy, `compileall`, and the complete pytest suite;
- manually verify one copy of the Backtester database without modifying it.

## Estimated effort

- reporting/query layer: 2-3 hours;
- Streamlit views and navigation: 2-3 hours;
- tests, documentation, and real-database validation: 1-2 hours.

Expected total: 5-8 hours. The first useful version can omit custom charts and ship in roughly
4 hours while retaining all principal tables and filters.

# Consult the AppMonitor database

The viewer is a local, read-only Streamlit application for inspecting persisted runs without
writing SQL manually.

## Install

Install the package with its optional viewer dependencies:

```powershell
uv sync --extra viewer
```

For a development checkout, `uv sync --group dev` also installs Streamlit so the UI test can run.
Streamlit is not part of AppMonitor's core runtime dependencies.

## Start the viewer

From the monitored repository, use the default database:

```powershell
uv run appmonitor-viewer
```

Or select another AppMonitor database explicitly:

```powershell
uv run appmonitor-viewer --database C:\projects\target\.appmonitor\runs.sqlite3
```

The command starts a local Streamlit server and prints its URL. Stop it with `Ctrl+C`. The selected
absolute database path is displayed below the AppMonitor title.

For Streamlit test harnesses and embedded launches, `APPMONITOR_VIEWER_DATABASE` supplies the
database when `--database` is absent.

## Read the tabs

### Overview

Start here to check process outcomes, LLM cost and reliability, patch decisions, pushes, and
recovery actions. The lower table contains the ten latest runs.

### Runs

Filter by outcome or search run IDs, repository paths, and serialized commands. Select a run to
inspect:

- the compact report summary;
- captured stdout and stderr;
- lifecycle state transitions;
- created, modified, and deleted artifact metadata.

Output content comes from the persisted report. Treat it as potentially sensitive operational
data.

### Runtime

Choose a run to view sample count, peak RSS, peak CPU, the metric timeline, and raw samples.
An empty state means that the run contains no process samples, not that the database is invalid.

### LLM

Each row represents one task/model pair. Compare:

- call count and structured success rate;
- invalid responses and provider errors;
- average latency;
- prompt and completion tokens;
- total USD cost.

These are the same task-level observations used by adaptive routing. Prompts, model responses, and
API credentials are not stored in `llm_calls`.

### Maintenance

This view correlates patch status and reason with the original run outcome. Use the raw
`run_patches` table when the complete plan, validation, reviewer JSON, or diff is needed.

### Git and recovery

Inspect the dedicated maintenance branch, base and resulting commits, remote push, restart or stop
decision, restarted run ID, and resulting outcome. A null remote or push field identifies the
default local-only workflow.

### Tables

Browse tables from AppMonitor's fixed allowlist and download the visible page as CSV. The page is
bounded and not editable. Arbitrary SQL is deliberately unsupported.

## Persistence and backups

Keep `.appmonitor/runs.sqlite3` on a persistent local directory or mounted service volume. Do not
commit the live database: SQLite produces non-mergeable binary churn, can grow continuously, and
may contain repository paths, commands, logs, and diagnostic metadata.

The viewer does not create a snapshot. For a consistent external backup while monitoring is
active, use SQLite's backup mechanism rather than copying a file during a write transaction.

## Troubleshooting

`not a SQLite database` means the selected file is empty or has an invalid header.

`invalid AppMonitor runs table` means the file is SQLite but does not expose the required core
schema. Optional phase tables may be absent and simply produce empty views.

`viewer dependency missing` means the core package was installed without `appmonitor[viewer]`.


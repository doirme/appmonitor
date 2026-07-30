# Model routing and observability

This tutorial explains what AppMonitor decides automatically, what it records, and how to inspect
the data before a dashboard exists.

## One registry fetch per explicit call

```python
config = OpenRouterConfig.from_env_file(".env.txt")
registry = fetch_model_registry(config)
client = OpenRouterClient(config=config, registry=registry)
```

`fetch_model_registry()` makes the catalog request. Constructing the client and calling
`complete_structured()` do not refresh it. Keep one registry for a bounded workflow; fetch again
when you intentionally want current prices and capabilities.

There is currently no disk cache, expiration time, or offline fallback. Production service work
should add a cached registry with a last-known-good snapshot.

## Follow one routing decision

For a request, the client:

1. serializes the messages and estimates tokens conservatively at roughly one token per three
   characters;
2. starts from models that cleared the reference context, cutoff, expiration, and optional
   coding-index policy during registry fetch;
3. requires the task's configured minimum context;
4. requires `structured_outputs` or `response_format`;
5. estimates worst-case cost from input estimate and maximum output;
6. applies reviewer allowlist, author-model, and critical provider exclusions when requested;
7. ranks models with sufficient task history by structured quality, reliability, latency, cost,
   and model ID;
8. gives models without sufficient history neutral priors, preserving cost order between them;
9. reserves budget before trying the first candidate;
10. tries the next candidate after provider or schema failure, within `max_attempts`.

Free models remain valid only when they clear the reference policy. History is isolated by task:
results for `run_critic` do not alter the rank for `patch_reviewer`.

## Put all evidence in one database

```python
from pathlib import Path

from appmonitor import (
    OpenRouterClient,
    SQLiteDiagnosticStore,
    SQLiteLLMTelemetry,
    SQLitePatchStore,
    SQLiteRegressionStore,
    SQLiteRunStore,
)

database = Path(".appmonitor/runs.sqlite3")
run_store = SQLiteRunStore(database)
telemetry = SQLiteLLMTelemetry(database)
diagnostic_store = SQLiteDiagnosticStore(database)
regression_store = SQLiteRegressionStore(database)
patch_store = SQLitePatchStore(database)

client = OpenRouterClient(
    config=config,
    registry=registry,
    telemetry=telemetry,
)
```

Sharing the path allows joins and one-file backup. Never point a maintenance store at a database
that does not contain its referenced run.

## Inspect calls from Python

```python
for call in telemetry.list_calls():
    print(
        call.task,
        call.model,
        call.status,
        f"{call.latency_seconds:.3f}s",
        f"${call.cost_usd:.6f}",
    )
```

This is the current high-level reporting API for model calls.

Use the same projection consumed by adaptive routing:

```python
for stats in telemetry.summarize("patch_reviewer"):
    print(
        stats.model,
        stats.samples,
        f"quality={stats.quality_rate:.1%}",
        f"reliability={stats.reliability_rate:.1%}",
        f"latency={stats.average_latency_seconds:.3f}s",
    )
```

## Build useful summaries with standard-library SQLite

```python
import sqlite3

database = ".appmonitor/runs.sqlite3"
connection = sqlite3.connect(database)
connection.row_factory = sqlite3.Row

rows = connection.execute(
    """
    SELECT
        task,
        model,
        COUNT(*) AS calls,
        SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS successes,
        ROUND(AVG(latency_seconds), 3) AS average_latency_seconds,
        SUM(prompt_tokens + completion_tokens) AS tokens,
        ROUND(SUM(cost_usd), 6) AS cost_usd
    FROM llm_calls
    GROUP BY task, model
    ORDER BY task, cost_usd
    """
).fetchall()

for row in rows:
    print(dict(row))
```

This exposes reliability, latency, and aggregate cost by task and model. The high-level
`summarize()` API uses equivalent aggregates for adaptive routing.

## Find expensive or failed attempts

```python
rows = connection.execute(
    """
    SELECT started_at, task, model, status, error_type,
           latency_seconds, prompt_tokens, completion_tokens, cost_usd
    FROM llm_calls
    WHERE status <> 'succeeded' OR cost_usd > 0.01
    ORDER BY started_at DESC
    """
).fetchall()
```

The table stores the prompt SHA-256 for correlation, but not the prompt or response text.

## Inspect runtime performance

```python
rows = connection.execute(
    """
    SELECT
        r.run_id,
        r.outcome,
        r.exit_code,
        ROUND((julianday(r.finished_at) - julianday(r.started_at)) * 86400, 3)
            AS duration_seconds,
        MAX(m.rss_bytes) AS peak_rss_bytes,
        ROUND(AVG(m.cpu_percent), 2) AS average_cpu_percent
    FROM runs AS r
    LEFT JOIN metrics AS m ON m.run_id = r.run_id
    GROUP BY r.run_id
    ORDER BY r.started_at DESC
    LIMIT 20
    """
).fetchall()
```

Use `log_lines` for failure excerpts and `artifacts` for changed-file evidence:

```python
run_id = rows[0]["run_id"]
errors = connection.execute(
    """
    SELECT timestamp, message
    FROM log_lines
    WHERE run_id = ? AND stream = 'stderr'
    ORDER BY sequence
    """,
    (run_id,),
).fetchall()
```

## What is not highlighted yet

Today the value is in durable evidence and reproducible SQL, not in presentation. Missing pieces
include:

- trend charts and run-to-run comparisons;
- cost and latency budgets by task;
- cost per accepted diagnosis, reproducing test, or approved patch;
- semantic quality outcomes beyond structured-response validity;
- a CLI report/export command and a web dashboard;
- registry caching and provider-health history.

A useful next observability phase would add read-only aggregate report objects first, test their
calculations, then expose the same projections through CLI JSON. That keeps presentation separate
from the evidence tables.

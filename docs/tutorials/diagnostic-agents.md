# Read-only diagnostic agents

The diagnostic pipeline separates interpretation from mutation. Its agents receive structured
facts and one capability only: bounded structured completion. They cannot execute commands, read
additional repository files, or write a patch.

## Pipeline

```python
from appmonitor import (
    DiagnosticPipeline,
    LLMBudget,
    SQLiteDiagnosticStore,
)

pipeline = DiagnosticPipeline(
    client=openrouter_client,
    store=SQLiteDiagnosticStore(".appmonitor/runs.sqlite3"),
)
diagnostic = pipeline.analyze(
    orchestrated_run,
    budget=LLMBudget(max_calls=4, max_cost_usd=0.02),
)
```

`RunCriticAgent` always runs first. It returns a `RunAssessment` containing a summary, goal
alignment, evidence-backed findings, an investigation decision, and confidence.

`IncidentAnalystAgent` runs only when at least one deterministic or structured trigger applies:

- the process did not succeed;
- the goal evaluation failed;
- the critic requested investigation;
- the critic reported a high or critical finding.

It returns classification, root-cause hypothesis, evidence, suspected files, reproduction steps,
priority, and confidence.

## Context boundary

`build_diagnostic_context()` includes:

- process outcome, exit status, duration, peak memory, and artifact paths;
- bounded stdout and stderr excerpts;
- deterministic goal checks;
- commit, branch, and dirty state;
- syntax errors, quality-tool outcomes, and symbol count.

It does not include source code, environment variables, artifact contents, or static-analysis
docstrings. Logs default to at most 40 lines per stream and 500 characters per line. Common API
key, token, password, and secret patterns are replaced with `[REDACTED]`.

## Model fallback

Each agent permits at most two ordered model attempts. If a provider returns malformed structured
output, the OpenRouter client tries the next compatible model in cost order. Every attempt consumes
the shared `LLMBudget` and receives an independent telemetry record.

## Persistence

`SQLiteDiagnosticStore` adds a one-to-one `run_diagnostics` row containing:

- assessment call ID and JSON;
- optional incident call ID and JSON.

The row references the existing `runs` record and is deleted with it.

## Real smoke test

On 2026-07-29, AppMonitor monitored a deliberate exit code 2 and diagnosed it through OpenRouter:

- run ID: `370098a5-9ad3-4a29-9c80-b4f2866bb32f`;
- one critic finding;
- incident classification: `runtime_error`;
- priority: `high`;
- critic confidence: 1.00;
- two model calls and USD 0 calculated cost.

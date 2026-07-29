# Goal contract tutorial

The goal contract converts an informal definition of success into deterministic checks. It does
not contain Python, shell commands, or LLM prompts. AppMonitor parses it as data with
`yaml.safe_load` and rejects every undocumented field.

## First run

The repository includes `examples/goal.yaml`:

```yaml
version: 1
process:
  exit_code: 0
events:
  stdout_contains:
    - Hello from AppMonitor
resources:
  max_runtime_seconds: 5
  max_peak_rss_mb: 128
```

Run the hello-world program against this contract:

```powershell
uv run appmonitor run `
  --repo . `
  --goal examples/goal.yaml `
  -- uv run python examples/hello_world/hello.py
```

The JSON output contains `goal.contract`, including the SHA-256 of the exact YAML bytes, and
`goal.evaluation`.

## Supported schema

| YAML field | Meaning |
| --- | --- |
| `version` | Required schema version; currently exactly `1` |
| `process.exit_code` | Expected integer process exit status |
| `artifacts.required` | Glob patterns for files created or modified by the run |
| `events.stdout_contains` | Substrings required in captured stdout lines |
| `events.stderr_contains` | Substrings required in captured stderr lines |
| `resources.max_runtime_seconds` | Inclusive wall-clock upper bound |
| `resources.max_peak_rss_mb` | Inclusive aggregate process-tree RSS upper bound |

All sections are optional except `version`. List values must contain non-empty strings, and
resource budgets must be positive numbers.

## Result semantics

Each check has one status:

- `passed`: the observation satisfies the criterion;
- `failed`: an available observation contradicts or misses the criterion;
- `unavailable`: AppMonitor could not collect the measurement.

The overall result is `failed` when any check fails, `partial` when no check fails but at least
one is unavailable, and `passed` otherwise. This evaluation is independent of the process
`RunOutcome`: a process may exit successfully but fail its functional goal.

## Artifact behavior

Artifact patterns use Unix-style globs such as `results/*.json`. They are matched against files
created or modified during the run. In a Git repository, ignored files are deliberately absent
from snapshots. A required generated result must therefore not be ignored by Git if it should be
evaluated as a monitored artifact.

## Persistence

The complete goal section is stored in `runs.report_json`. The normalized `run_goals` table also
stores:

- `contract_sha256`;
- normalized `contract_json`;
- deterministic `evaluation_json`.

This makes it possible to compare evaluations while proving which exact contract was used.

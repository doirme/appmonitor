# CLI reference

```text
appmonitor run [OPTIONS] -- COMMAND [ARGS...]
```

Example:

```bash
appmonitor run --repo ./project --timeout 300 --goal goal.yaml -- python main.py
```

| Option | Meaning |
| --- | --- |
| `--repo PATH` | Working repository; defaults to the current directory |
| `--timeout SECONDS` | Positive runtime limit |
| `--sync-environment` | Run `uv sync --frozen` before the target |
| `--analyze` | Run AST indexing and the fixed quality suite |
| `--goal PATH` | Load and evaluate a version-one goal contract |
| `--` | Optional separator before the monitored command |

The command prints one enriched JSON report and persists it to
`<repository>/.appmonitor/runs.sqlite3`. Its exit code is currently zero after any successfully
observed target run, even when the target itself failed or timed out. Startup and configuration
errors propagate.

The CLI currently exposes deterministic monitoring only. Diagnostic, regression, patching, and
reporting stages are Python APIs.

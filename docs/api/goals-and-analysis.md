# Goals and static analysis

## Goal contracts

`load_goal_contract(path) -> GoalContract` uses safe YAML loading and a closed version-one schema.
Unknown fields, unsupported versions, malformed YAML, and invalid value types raise
`GoalContractError`.

`GoalContract` fields:

| Field | Type |
| --- | --- |
| `sha256` | `str` |
| `exit_code` | `int | None` |
| `required_artifacts` | `tuple[str, ...]` |
| `stdout_contains` | `tuple[str, ...]` |
| `stderr_contains` | `tuple[str, ...]` |
| `max_runtime_seconds` | `float | None` |
| `max_peak_rss_mb` | `float | None` |

`GoalEvaluator.evaluate(contract, report) -> GoalEvaluation` performs deterministic comparisons.
Each `GoalCheck` is `passed`, `failed`, or `unavailable`. `GoalEvaluation.overall` is `passed`,
`partial`, or `failed`; `to_dict()` returns portable data.

See the [goal tutorial](../tutorials/goal-contract.md) for the YAML schema.

## Static analysis

```python
report = StaticAnalyzer(run_tools=False).analyze(repository_path)
```

The default AST pass reads Python files without importing them. `StaticAnalysisReport` contains:

- `symbols`: classes, sync functions, and async functions with qualified name, signature,
  annotation, docstring, path, and line;
- `imports`: normalized imports and their locations;
- `syntax_errors`: decoding and Python syntax findings that do not stop other files;
- `tools`: optional deterministic command results.

`to_dict()` converts the complete report to JSON-compatible data.

With `run_tools=True`, the fixed command set is:

```text
uv run ruff check . --output-format json
uv run mypy .
uv run python -m compileall -q .
uv run pytest --collect-only -q
uv run pytest --cov --cov-branch --cov-report=json -q
```

Each `ToolCheck` records its name, argument vector, status, exit code, stdout, and stderr.
Exit code zero is `passed`, 127 is `unavailable`, and every other result is `failed`. Tool failure
is evidence in the report, not an exception from `analyze`.

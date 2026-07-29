# Bounded regression-test generation

The test architect is a proposal service, not a filesystem actor. AppMonitor separates generation,
authorization, writing, and verification into independent steps.

## Workflow

```python
from appmonitor import (
    GeneratedTestPolicy,
    LLMBudget,
    RegressionTestGenerator,
    RegressionTestWorkflow,
    SQLiteRegressionStore,
)

workflow = RegressionTestWorkflow(
    generator=RegressionTestGenerator(openrouter_client),
    policy=GeneratedTestPolicy(),
    store=SQLiteRegressionStore(".appmonitor/runs.sqlite3"),
)
result = workflow.generate(
    run,
    diagnostic,
    source_paths=("src/project/calculator.py",),
    budget=LLMBudget(max_calls=4, max_cost_usd=0.02),
)
```

`collect_source_context()` reads at most four explicitly named repository-local Python files and
at most 20,000 characters. The model receives only those excerpts and the structured diagnostic.

## Local authorization

The generated proposal has four schema-validated fields: path, Python content, target behavior,
and rationale. `GeneratedTestPolicy` then parses the content with `ast` and rejects it unless:

- the path is a new relative `tests/**/test_*.py` file;
- it stays below the real repository `tests` directory without symlink traversal;
- the content is valid Python below 32 KiB;
- it defines a `test_*` function;
- it contains an `assert` or `pytest.raises`.

The policy rejects network and process imports, dynamic execution builtins, dunder access, and
direct filesystem creation, writing, renaming, permission changes, or deletion.

## Reproduction proof

After exclusive file creation, the workflow executes exactly:

```text
uv run python -m pytest --rootdir=. <generated-test-path> -q
```

The subprocess has a hard 120-second timeout. Only pytest exit code `1` means the regression is
reproduced. Exit code `0` means the test does not reproduce; collection, usage, internal, timeout,
and unavailable-tool statuses are verification errors. Every non-reproducing file is deleted.

## Persistence

`SQLiteRegressionStore` records the run ID, path, content SHA-256, intended behavior, rationale,
status, and pytest exit code. It does not duplicate the generated source content in SQLite.

## Real smoke test

AppMonitor was tested against a synthetic `calculator.add()` implementation that subtracted its
operands:

- run ID: `19234866-05e3-4c40-8422-1787118b5aa1`;
- generated path: `tests/test_calculator.py`;
- verification: one failed test, pytest exit code `1`;
- model attempts: 2;
- calculated cost: USD 0.00008825.

The generated test asserted `calculator.add(2, 3) == 5` and was retained.

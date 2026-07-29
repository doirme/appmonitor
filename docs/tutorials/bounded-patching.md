# Bounded transactional patching

Phase 8 changes existing local source files but still does not perform any Git operation. Planning,
implementation, deterministic verification, and review remain separate authorities.

## Pipeline

```python
from appmonitor import (
    LLMBudget,
    PatchImplementerAgent,
    PatchPipeline,
    PatchPlannerAgent,
    PatchReviewerAgent,
    SQLitePatchStore,
)

pipeline = PatchPipeline(
    planner=PatchPlannerAgent(openrouter_client),
    implementer=PatchImplementerAgent(openrouter_client),
    reviewer=PatchReviewerAgent(reviewer_client),
    store=SQLitePatchStore(".appmonitor/runs.sqlite3"),
)
result = pipeline.execute(
    run,
    diagnostic,
    regression,
    source_paths=("src/project/calculator.py",),
    budget=LLMBudget(max_calls=6, max_cost_usd=0.05),
)
```

The pipeline requires a regression result with status `reproduces` and a non-empty explicit source
scope.

## Three roles

`PatchPlannerAgent` receives the diagnostic, reproducing test metadata, and bounded source context.
Its JSON Schema binds every planned path to an enum of the supplied source paths.

`PatchImplementerAgent` receives only the approved plan and source snapshots. It returns complete
file replacements with the exact SHA-256 of the original bytes. Its schema binds replacement paths
to the planner's paths.

`PatchReviewerAgent` runs only after deterministic validation. It sees the plan, unified diff, and
validation results, not the implementer's conversation. It returns `approve` or `reject`.

## Local policy

`PatchPolicy` permits:

- one to three explicitly supplied existing `.py` source files;
- no files under `tests/`;
- no absolute path, traversal, or symlink;
- exact original-byte SHA-256 agreement;
- valid UTF-8 and Python syntax;
- at most 64 KiB per replacement;
- at most 200 added plus removed lines across the patch.

It rejects duplicate paths, unchanged replacements, new files, test changes, stale hashes, and
invalid syntax before mutation.

## Transaction and rollback

`AtomicPatchApplier` writes a temporary sibling file, flushes and fsyncs it, preserves permission
bits, then atomically replaces the target. The transaction restores original bytes in reverse order
unless `commit()` is explicitly called after all approvals. Here, commit means keep the local
working-tree bytes; it is not a Git commit.

## Fixed validation

The verifier stops at the first failure:

```text
uv run python -m pytest --rootdir=. <regression-test> -q
uv run python -m pytest --rootdir=. -q
uv run ruff check .
uv run mypy .
uv run python -m compileall -q -x <cache-and-environment-regex> .
```

Every command uses an argument vector without a shell and a 120-second timeout. Validation failure
or reviewer rejection rolls the patch back.

## Persistence

`SQLitePatchStore` records the final status and reason, plan JSON, patch SHA-256, unified diff,
validation JSON, and optional review JSON against the exact source run.

## Real smoke test

On 2026-07-29, AppMonitor repaired the synthetic calculator:

- run ID: `050c862b-b7f3-4660-b93f-c1d6d7e6bc89`;
- changed `return left - right` to `return left + right`;
- regression, complete pytest suite, Ruff, mypy, and compilation all passed;
- independent review verdict: `approve`;
- three OpenRouter calls;
- calculated cost: USD 0.001578;
- patch SHA-256: `01fec79a34ec951d5da8a9cf8b471b555dddce2915f01b7440f3e9344b83e6cd`.

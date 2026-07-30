# Diagnostic and maintenance pipelines

Every LLM role receives only `complete_structured`. Deterministic code retains authority over
context selection, budgets, filesystem access, execution, and persistence.

## Diagnostics

`DiagnosticPipeline(client=..., store=None).analyze(run, budget=...) -> DiagnosticResult` always
invokes the run critic. It invokes the incident analyst when the run failed, the deterministic goal
failed, the critic requests investigation, or a finding is high/critical.

`DiagnosticResult` contains a required `RunAssessment` and call ID plus optional
`IncidentAnalysis` and call ID. Findings include category, severity, summary, evidence,
recommendation, and confidence.

`build_diagnostic_context(run, max_log_lines=40, max_line_chars=500)` projects runtime, goal,
repository, and static-analysis facts. It bounds logs, redacts common secrets, and excludes source
content, artifact content, environment variables, and docstrings.

`SQLiteDiagnosticStore.save(run_id, result)` inserts one immutable `run_diagnostics` row.

## Regression generation

`collect_source_context(repository, paths)` reads an explicit bounded set of repository-local
Python source files. Escape paths and size/count violations raise `SourceContextError`.

`RegressionTestGenerator.propose(diagnostic, source_context=..., budget=...) -> TestProposal` asks
for one structured test proposal without write access.

`GeneratedTestPolicy.validate(repository, proposal) -> Path` requires:

- a new repository-local `tests/**/test_*.py` path;
- valid Python within the byte limit;
- at least one `test_*` function and a real assertion or `pytest.raises`;
- no prohibited network/process imports, dynamic execution, dunder access, skips, xfails, or
  direct filesystem mutations.

Violations raise `TestPolicyError` before writing.

`RegressionTestWorkflow(generator=..., policy=None, runner=None, store=None).generate(...)` collects
source, requests a proposal, authorizes it, exclusively creates the file, and runs one fixed pytest
command with a 120-second limit. Only pytest exit code 1 produces status `reproduces` and retains
the file. Every other result removes it.

`RegressionTestResult` exposes the proposal, status, pytest exit code, streams, `content_sha256`,
and `to_dict()`. `SQLiteRegressionStore.save()` persists the audit result.

## Bounded patching

Construct separate `PatchPlannerAgent`, `PatchImplementerAgent`, and `PatchReviewerAgent` over the
structured client, then inject them into `PatchPipeline`.

```python
result = pipeline.execute(
    run,
    diagnostic,
    regression,
    source_paths=("src/example/calculator.py",),
    budget=budget,
)
```

Execution requires a proven `reproduces` regression and an explicit non-empty source scope.
`PatchPolicy.authorize()` checks existing non-test Python paths, exact original-byte SHA-256,
valid syntax, unchanged/stale content, file count, bytes, and changed-line limits. Policy failures
raise `PatchPolicyError` before mutation.

`AtomicPatchApplier` creates a rollback-by-default transaction. The verifier runs, in order:

1. the generated regression;
2. the full pytest suite;
3. Ruff;
4. mypy;
5. Python compilation.

Checks stop at the first failure. The independent reviewer runs only after all deterministic checks
pass. The transaction commits only for reviewer verdict `approve`; otherwise exact original bytes
are restored.

`PatchPipelineResult` includes `status`, `reason`, plan, patch hash, unified diff, validation, and
optional review. `SQLitePatchStore.save()` persists the complete decision. The pipeline performs no
Git, branch, commit, push, or pull-request operation.

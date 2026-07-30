# End-to-end maintenance workflow

This is the current application-level sequence. Run it first on a disposable repository: the
regression and patch stages intentionally write files when their policies approve.

## 1. Monitor and persist

```python
from pathlib import Path

from appmonitor import RunClient, RunSpec, SQLiteRunStore

repository = Path("../target-project").resolve()
database = repository / ".appmonitor" / "runs.sqlite3"
run_store = SQLiteRunStore(database)

run = RunClient(store=run_store).execute(
    RunSpec(
        repository=repository,
        command=("uv", "run", "python", "research/example.py"),
        timeout_seconds=120,
        analyze_repository=True,
    )
)

print(run.run_id, run.report.outcome, run.report.duration_seconds)
```

Start without `sync_environment=True` when observing an existing environment. Enable it only when
the target repository has a valid `uv.lock` and you want `uv sync --frozen`.

## 2. Build the bounded model client

```python
from appmonitor import (
    LLMBudget,
    OpenRouterClient,
    OpenRouterConfig,
    SQLiteLLMTelemetry,
    fetch_model_registry,
)

config = OpenRouterConfig.from_env_file(".env.txt")
registry = fetch_model_registry(config)
telemetry = SQLiteLLMTelemetry(database)
llm = OpenRouterClient(config=config, registry=registry, telemetry=telemetry)
budget = LLMBudget(max_calls=8, max_cost_usd=0.10)
```

The same mutable budget is intentionally shared by all following agents. Raise it only after
reviewing actual call telemetry.

## 3. Diagnose without mutation

```python
from appmonitor import DiagnosticPipeline, SQLiteDiagnosticStore

diagnostic = DiagnosticPipeline(
    client=llm,
    store=SQLiteDiagnosticStore(database),
).analyze(run, budget=budget)

print(diagnostic.assessment.summary)
if diagnostic.incident:
    print(diagnostic.incident.root_cause)
```

Stop here when you only need monitoring and advice. This phase cannot read source or write files.

## 4. Prove the issue with a generated test

```python
from appmonitor import (
    RegressionTestGenerator,
    RegressionTestWorkflow,
    SQLiteRegressionStore,
)

regression_workflow = RegressionTestWorkflow(
    generator=RegressionTestGenerator(llm),
    store=SQLiteRegressionStore(database),
)
regression = regression_workflow.generate(
    run,
    diagnostic,
    source_paths=("src/example/calculator.py",),
    budget=budget,
)

print(regression.status, regression.proposal.path)
```

Continue only when status is `reproduces`. The workflow deletes tests that pass, cannot execute,
time out, or violate policy.

## 5. Apply a bounded transactional patch

```python
from appmonitor import (
    PatchImplementerAgent,
    PatchPipeline,
    PatchPlannerAgent,
    PatchReviewerAgent,
    SQLitePatchStore,
)

patching = PatchPipeline(
    planner=PatchPlannerAgent(llm),
    implementer=PatchImplementerAgent(llm),
    reviewer=PatchReviewerAgent(llm),
    store=SQLitePatchStore(database),
)

patch = patching.execute(
    run,
    diagnostic,
    regression,
    source_paths=("src/example/calculator.py",),
    budget=budget,
)

print(patch.status, patch.reason)
print(patch.diff)
```

An `applied` result means the regression, full tests, Ruff, mypy, compilation, and independent
review all accepted the local bytes. A `rejected` result has already restored the originals.

## 6. Use the isolated maintenance workflow

Steps 4 and 5 demonstrate the regression and patch APIs separately. For an actual V1 maintenance
run, do not execute those local steps first. Give both configured stages to
`GitMaintenanceWorkflow`, which runs them in a detached worktree and commits only after acceptance.

```python
from appmonitor import GitMaintenanceWorkflow, SQLiteGitStore

git_result = GitMaintenanceWorkflow(
    regression_workflow=regression_workflow,
    patch_pipeline=patching,
    store=SQLiteGitStore(database),
).execute(
    run,
    diagnostic,
    source_paths=("src/example/calculator.py",),
    budget=budget,
)

print(git_result.status, git_result.branch, git_result.commit)
```

Add `git_remote="origin"` to the original `RunSpec` to require dedicated-branch publication.
Pass a `restart_spec` to `execute()` to run the accepted version locally. See the
[Git worktree tutorial](git-worktrees.md) for preflight, rejection, recovery limits, and
long-running server behavior.

## 7. Inspect the audit trail

Read [model routing and observability](model-routing-and-observability.md) to summarize runtime,
model attempts, costs, diagnostics, regression evidence, and the patch decision from the shared
SQLite database.

The SQLite database remains under the source repository's ignored `.appmonitor/` directory.
Pull-request creation and deployment remain outside this workflow.

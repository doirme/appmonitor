# Commit verified maintenance in an isolated worktree

Start from a persisted monitored run and its read-only diagnostic. The source repository must be
clean; `.appmonitor/` state is ignored.

```python
from appmonitor import (
    GitMaintenanceWorkflow,
    PatchImplementerAgent,
    PatchPipeline,
    PatchPlannerAgent,
    PatchReviewerAgent,
    RegressionTestGenerator,
    RegressionTestWorkflow,
    SQLiteGitStore,
)

regression_workflow = RegressionTestWorkflow(
    generator=RegressionTestGenerator(llm),
)
patch_pipeline = PatchPipeline(
    planner=PatchPlannerAgent(llm),
    implementer=PatchImplementerAgent(llm),
    reviewer=PatchReviewerAgent(llm),
)
git_maintenance = GitMaintenanceWorkflow(
    regression_workflow=regression_workflow,
    patch_pipeline=patch_pipeline,
    store=SQLiteGitStore(database),
)

result = git_maintenance.execute(
    run,
    diagnostic,
    source_paths=("src/example/calculator.py",),
    budget=budget,
)
```

On acceptance:

```python
print(result.status)
print(result.branch)
print(result.commit)
print(result.changed_paths)
```

The original checkout remains unchanged. Inspect the committed result with:

```bash
git show appmonitor/<run-id>
```

The temporary worktree has already been removed. The branch and commit remain local.

Do not run the standalone regression and patch tutorials against the source checkout immediately
before this workflow. `GitMaintenanceWorkflow` invokes those stages itself inside isolation.

## Rejection behavior

`result.status == "rejected"` means either the test did not reproduce the problem or patch
validation/review rejected the change. No branch or commit is created and temporary bytes are
removed with the worktree.

Infrastructure and policy errors raise `GitAutomationError`, for example:

- dirty source repository;
- unsafe or duplicate run branch;
- invalid authorized path;
- unexpected changed file;
- empty commit;
- failed Git command.

The workflow deliberately has no push or pull-request method.

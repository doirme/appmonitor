# Publish and restart verified maintenance

Start from a persisted monitored run and its read-only diagnostic. The source repository must be
clean; `.appmonitor/` state is ignored.

Local-only mode is the default. To require remote publication, opt in before the monitored run:

```python
run = RunClient().execute(
    RunSpec(
        repository=repository,
        command=("python", "server.py"),
        git_remote="origin",
    )
)
```

AppMonitor refuses to start the command if the remote, credentials, dry-run push, or dedicated
branch check fails. Remove `git_remote` to continue with local-only maintenance.

```python
from appmonitor import (
    GitMaintenanceWorkflow,
    PatchImplementerAgent,
    PatchPipeline,
    PatchPlannerAgent,
    PatchReviewerAgent,
    RegressionTestGenerator,
    RegressionTestWorkflow,
    RecoveryDecisionAgent,
    RecoveryLimits,
    RunClient,
    RunSpec,
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
    decision_maker=RecoveryDecisionAgent(llm),
    restart_limits=RecoveryLimits(max_restarts=3, max_duration_seconds=30 * 60),
)

result = git_maintenance.execute(
    run,
    diagnostic,
    source_paths=("src/example/calculator.py",),
    budget=budget,
    restart_spec=RunSpec(
        repository=repository,
        command=("python", "server.py"),
        timeout_seconds=None,
    ),
)
```

On acceptance:

```python
print(result.status)
print(result.branch)
print(result.commit)
print(result.changed_paths)
print(result.remote, result.pushed)
print(result.restart_decision)
print(result.restart_run_id, result.restart_outcome)
```

The original checkout remains unchanged. Inspect the committed result with:

```bash
git show appmonitor/<run-id>
```

The temporary worktree is removed after the restarted command exits. For a long-running server
without a timeout, it remains the server's working copy for that process lifetime.

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

There is deliberately no pull-request, deployment, Portainer, traffic-switching, or rollback
method in this phase.

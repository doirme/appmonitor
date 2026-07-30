# Isolated Git maintenance

The V1 Git boundary creates a local branch and commit only after the existing regression and patch
pipelines accept their work. It never pushes, opens a pull request, rebases, or modifies the source
checkout.

## `GitMaintenanceWorkflow`

```python
workflow = GitMaintenanceWorkflow(
    regression_workflow=regression_workflow,
    patch_pipeline=patch_pipeline,
    worktrees=None,
    store=SQLiteGitStore(database),
)

result = workflow.execute(
    run,
    diagnostic,
    source_paths=("src/example.py",),
    budget=budget,
    commit_message=None,
)
```

The workflow:

1. requires a valid clean Git repository, ignoring only AppMonitor's `.appmonitor/` state;
2. resolves the exact current `HEAD`;
3. creates a detached worktree at `.appmonitor/worktrees/<run-id>`;
4. projects the existing `OrchestratedRun` onto the worktree path;
5. generates and proves the regression inside the worktree;
6. applies and verifies the bounded patch inside the worktree;
7. rejects any changed path outside `source_paths` and the generated regression path;
8. creates `appmonitor/<run-id>` and one local commit;
9. removes the worktree while retaining the accepted branch and commit.

A non-reproducing regression or rejected patch removes the detached worktree without creating a
branch or commit.

`GitMaintenanceResult` contains `status`, `reason`, branch, base commit, optional commit, changed
paths, regression result, and optional patch result. `status` is `committed` or `rejected`.

## `GitWorktreeManager`

`prepare(repository, run_id=...) -> PreparedWorktree` validates the run ID, clean base, branch
availability, managed path, and worktree creation.

`commit(worktree, allowed_paths=..., message=...) -> GitCommitResult` validates repository-local
POSIX paths, rejects scope creep and empty changes, creates the branch, stages exactly the observed
allowed paths, commits, and resolves the commit SHA.

`cleanup(worktree)` only removes paths beneath the repository's managed
`.appmonitor/worktrees` root.

All Git operations use argument vectors through `CommandRunner`; no shell command is constructed.
Failures raise `GitAutomationError`.

## Persistence

`SQLiteGitStore(path).save(run_id, result)` writes one immutable `run_git_maintenance` row with the
base, branch, commit, changed paths, regression, patch, and final decision. `load(run_id)` returns
the portable JSON mapping or raises `KeyError`.

The database must contain the referenced run. Foreign keys and cascade deletion are enabled.

## V1 boundary

Automatic push, pull-request creation, remote branch policies, and human approval transport are
later opt-in capabilities. The local commit is the final V1 state.

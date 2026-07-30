# Isolated Git maintenance, publication, and restart

The Git boundary always creates a dedicated branch after the regression and patch pipelines accept
their work. Local-only operation is the default. An explicit remote enables preflight and push; no
mode rebases, force-pushes, or modifies the source checkout.

## Startup remote preflight

`RunSpec(git_remote=None)` is local-only. Set a safe remote name to opt in:

```python
spec = RunSpec(
    repository=repository,
    command=("python", "server.py"),
    git_remote="origin",
)
```

Before starting the target, `RunClient` asks `GitRemotePublisher.preflight()` to verify:

1. the repository and remote exist;
2. `appmonitor/<run-id>` does not already exist remotely;
3. authentication and push permission accept an exact `git push --dry-run`.

A failed check raises `GitAutomationError` before target execution. The error explains that
`git_remote=None` keeps local commit and restart behavior without requiring remote access. The
real push is checked again because permissions and branch state can change after startup.

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
    restart_spec=None,
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
9. pushes only that branch when the source run opted into a remote;
10. optionally obtains a structured restart/stop recommendation;
11. optionally reruns the target from the corrected worktree;
12. removes the worktree after the restarted command exits.

A non-reproducing regression or rejected patch removes the detached worktree without creating a
branch or commit.

`GitMaintenanceResult` contains `status`, reason, branch, base commit, optional commit, changed
paths, regression, patch, remote/push facts, recovery decision, and optional restarted run identity
and outcome. `status` is `committed`, `pushed`, or `rejected`.

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

## `GitRemotePublisher`

`preflight(repository, run_id=..., remote=...)` is read-only except for Git's protocol exchange. It
uses a dry-run push and refuses an existing branch. `publish(repository, commit, remote=...)`
pushes the exact local `appmonitor/<run-id>` ref without `--force`.

Git server policy can still change between preflight and publication. A real push denial therefore
remains an explicit workflow error and never falls back to another branch.

## Controlled local restart

Pass a `restart_spec` to run the accepted version from the corrected worktree. The default
`RecoveryLimits` permits three restarts over 30 minutes. Either limit may be `None`; the same
`LLMBudget` remains the cost and call-count authority across maintenance and recovery decisions.

Without a decision agent, a verified patch is restarted. Inject `RecoveryDecisionAgent` to obtain
a schema-validated `restart` or `stop` recommendation. Rejected patches never restart. Budget or
restart-limit exhaustion becomes a deterministic stop decision.

Restart runs are persisted in the source repository's `.appmonitor/runs.sqlite3`, not in the
disposable worktree. A server with `timeout_seconds=None` keeps the workflow and worktree alive for
the lifetime of that server.

## Persistence

`SQLiteGitStore(path).save(run_id, result)` writes one immutable `run_git_maintenance` row with the
base, branch, commit, changed paths, regression, patch, remote, push, restart decision, restarted
run, and final decision. Existing V1 tables are migrated with additive columns.

The database must contain the referenced run. Foreign keys and cascade deletion are enabled.

## Current boundary

Pull-request creation, deployment, Portainer orchestration, image rebuilding, live event-driven
interruption, traffic switching, and rollback are not part of this phase.

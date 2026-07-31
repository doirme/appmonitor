# Use-case map and safety boundaries

This page helps choose the smallest AppMonitor capability that fits the situation. AppMonitor is
deliberately layered: each step adds authority and therefore adds operational risk.

## Choose a workflow

| Need | Recommended entry point | Writes or executes target code? |
| --- | --- | --- |
| Observe one local command | `RunClient.execute(RunSpec(...))` or `appmonitor run` | Executes the target; persists evidence |
| Observe a function inside a Python process | `@monitored` | Executes the function; optionally persists an observation |
| Check repository structure and fixed tools | `StaticAnalyzer.analyze()` or `RunSpec(analyze_repository=True)` | Runs the configured analysis commands when enabled |
| Evaluate explicit success criteria | `load_goal_contract()` and `GoalEvaluator.evaluate()` | No mutation; evaluates an existing report |
| Ask what went wrong | `DiagnosticPipeline.analyze()` | LLM calls only; no source reads or file writes |
| Prove a suspected regression | `RegressionTestWorkflow.generate()` | May create a new test temporarily; removes it unless it reproduces |
| Apply a bounded repair | `PatchPipeline.execute()` | May modify only authorized existing source files; rolls back on rejection |
| Commit and optionally publish a verified repair | `GitMaintenanceWorkflow.execute()` | Uses a detached worktree and creates a dedicated branch |
| Restart the corrected version | `GitMaintenanceWorkflow.execute(..., restart_spec=...)` | Executes the restart command from the corrected worktree |
| Inspect historical operations | `ReportDatabase` or `appmonitor-viewer` | Read-only database access |

## Recommended progression

For a new repository, start with a monitored run and inspect its report. Add static analysis and a
goal contract when the expected outcome can be stated deterministically. Add diagnostics only when
structured interpretation is useful. Add regression generation and patching only after the issue
has enough evidence to define an explicit source scope.

The full maintenance sequence is:

```text
run -> deterministic analysis -> goal evaluation -> diagnosis
     -> regression proof -> bounded patch -> independent review
     -> isolated commit -> optional publication -> optional restart
```

Each arrow is optional. For example, a failed production command may need only diagnosis, while a
local code defect may need the complete sequence.

## What each layer is allowed to do

### Deterministic monitoring

`RunClient` and `LocalExecutor` own process execution, timeout handling, output capture, resource
samples, artifact changes, state transitions, and SQLite persistence. Their results do not depend
on an LLM.

### In-process instrumentation

`monitored` observes a function call without changing its return value or exception behavior. It
is useful for function-level evidence, but it cannot observe import failures, child processes, or
an interpreter that exits abruptly. It is synchronous and does not sample peak memory inside the
call.

### LLM-assisted interpretation

Diagnostic, regression, patch, and recovery agents receive structured completions only. The
deterministic layer controls budgets, schemas, source scope, filesystem access, commands, and
persistence. A model recommendation is never by itself permission to mutate a repository.

### Git maintenance

`GitMaintenanceWorkflow` is the boundary for accepted changes. It works in a managed detached
worktree, checks exact changed paths, creates a dedicated local branch, and optionally publishes
that branch. Pull-request creation, deployment, traffic switching, rollback, and interruption of
a still-running process are outside the current boundary.

### Reporting

`ReportDatabase` and the optional Streamlit viewer are strictly read-only. They tolerate absent
optional phase tables so that older databases remain inspectable, but they do not repair malformed
databases or perform schema migrations.

## Safe stopping points

- Stop after the monitored run when you need evidence only.
- Stop after diagnosis when you need an explanation or recommendation.
- Stop after regression generation when you want a durable proof before changing code.
- Stop after patch review when you want a verified local change without Git automation.
- Use the Git workflow only when an isolated commit or restart is explicitly wanted.
- Enable a remote only when branch publication is intended; local-only operation is the default.

## Current limitations

AppMonitor does not currently provide a service daemon, asynchronous event stream, live process
interruption, pull-request creation, Docker/Portainer deployment, traffic management, or automatic
rollback. These are architectural extensions rather than hidden behaviors of the current APIs.

For detailed contracts and failure behavior, continue with the relevant [API reference](../api/index.md)
or the [end-to-end maintenance tutorial](end-to-end-maintenance.md).

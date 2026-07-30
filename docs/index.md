# AppMonitor documentation

AppMonitor observes a Python command, preserves deterministic evidence, and then exposes bounded
LLM-assisted maintenance stages. Start with execution before enabling model calls or mutation.

## Learning path

1. [Project configuration and first monitored run](tutorials/pyproject-and-first-run.md)
2. [Instrument a Python function](tutorials/instrumentation.md)
3. [Goal contracts](tutorials/goal-contract.md)
4. [OpenRouter model routing](tutorials/openrouter.md)
5. [Reading runtime and LLM telemetry](tutorials/model-routing-and-observability.md)
6. [Read-only diagnostic agents](tutorials/diagnostic-agents.md)
7. [Regression-test generation](tutorials/regression-tests.md)
8. [Bounded transactional patching](tutorials/bounded-patching.md)
9. [Commit verified maintenance in a Git worktree](tutorials/git-worktrees.md)
10. [End-to-end maintenance workflow](tutorials/end-to-end-maintenance.md)

## Reference

- [API overview](api/index.md)
- [Execution and reports](api/execution.md)
- [In-process instrumentation](api/instrumentation.md)
- [Orchestration, repositories, and state](api/orchestration.md)
- [Goals and static analysis](api/goals-and-analysis.md)
- [OpenRouter, routing, budgets, and telemetry](api/openrouter.md)
- [Diagnostic and maintenance pipelines](api/maintenance.md)
- [Isolated Git maintenance](api/git-workflow.md)
- [SQLite persistence and schema](api/persistence.md)
- [CLI](api/cli.md)

## Project records

- [Initial architecture and delivery plan](initial-plan.md)
- [Implementation status](implementation-status.md)
- [Backtester trial](trials/backtester-world-lms.md)

The package root documented in the API reference is the supported public surface. Names imported
from internal modules can change until they are promoted to `appmonitor.__init__`.

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
9. [Publish and restart verified maintenance](tutorials/git-worktrees.md)
10. [End-to-end maintenance workflow](tutorials/end-to-end-maintenance.md)
11. [Consult the SQLite database](tutorials/sqlite-viewer.md)
12. [Use-case map and safety boundaries](tutorials/use-cases-and-boundaries.md)

## Reference

- [API overview](api/index.md)
- [Execution and reports](api/execution.md)
- [In-process instrumentation](api/instrumentation.md)
- [Orchestration, repositories, and state](api/orchestration.md)
- [Goals and static analysis](api/goals-and-analysis.md)
- [OpenRouter, routing, budgets, and telemetry](api/openrouter.md)
- [Diagnostic and maintenance pipelines](api/maintenance.md)
- [Git publication and controlled local restart](api/git-workflow.md)
- [SQLite persistence and schema](api/persistence.md)
- [Reporting and SQLite viewer](api/reporting.md)
- [CLI](api/cli.md)

## Project records

- [Initial architecture and delivery plan](initial-plan.md)
- [Implementation status](implementation-status.md)
- [SQLite viewer implementation plan](plans/sqlite-viewer.md)
- [Backtester trial](trials/backtester-world-lms.md)

The package root documented in the API reference is the supported public surface. Names imported
from internal modules can change until they are promoted to `appmonitor.__init__`.

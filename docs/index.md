# AppMonitor documentation

AppMonitor observes a Python command, preserves deterministic evidence, and then exposes bounded
LLM-assisted maintenance stages. Start with execution before enabling model calls or mutation.

## Learning path

1. [Project configuration and first monitored run](tutorials/pyproject-and-first-run.md)
2. [Goal contracts](tutorials/goal-contract.md)
3. [OpenRouter model routing](tutorials/openrouter.md)
4. [Reading runtime and LLM telemetry](tutorials/model-routing-and-observability.md)
5. [Read-only diagnostic agents](tutorials/diagnostic-agents.md)
6. [Regression-test generation](tutorials/regression-tests.md)
7. [Bounded transactional patching](tutorials/bounded-patching.md)
8. [End-to-end maintenance workflow](tutorials/end-to-end-maintenance.md)

## Reference

- [API overview](api/index.md)
- [Execution and reports](api/execution.md)
- [Orchestration, repositories, and state](api/orchestration.md)
- [Goals and static analysis](api/goals-and-analysis.md)
- [OpenRouter, routing, budgets, and telemetry](api/openrouter.md)
- [Diagnostic and maintenance pipelines](api/maintenance.md)
- [SQLite persistence and schema](api/persistence.md)
- [CLI](api/cli.md)

## Project records

- [Initial architecture and delivery plan](initial-plan.md)
- [Implementation status](implementation-status.md)
- [Backtester trial](trials/backtester-world-lms.md)

The package root documented in the API reference is the supported public surface. Names imported
from internal modules can change until they are promoted to `appmonitor.__init__`.

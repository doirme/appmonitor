# AppMonitor

AppMonitor is a Python library and command-line tool for observing Python programs at
runtime, producing reproducible diagnostics, and coordinating bounded maintenance work.

The architecture starts with a deterministic execution core. LLM agents, OpenRouter
routing, Git worktrees, and Docker isolation are added on top of that auditable base.

## Development

```bash
uv sync --dev
uv run pytest --cov --cov-branch
uv run ruff check .
uv run mypy src tests
uv run python -m compileall -q src tests
```

## Documentation

- [Documentation home and learning path](docs/index.md)
- [Initial architecture and delivery plan](docs/initial-plan.md)
- [API reference](docs/api/index.md)
- [Implementation status and next phases](docs/implementation-status.md)
- [French tutorial: `pyproject.toml`, tools, code reading, and first run](docs/tutorials/pyproject-and-first-run.md)
- [In-process instrumentation tutorial](docs/tutorials/instrumentation.md)
- [Goal contract tutorial](docs/tutorials/goal-contract.md)
- [OpenRouter foundation tutorial](docs/tutorials/openrouter.md)
- [Model routing and observability tutorial](docs/tutorials/model-routing-and-observability.md)
- [Read-only diagnostic agents tutorial](docs/tutorials/diagnostic-agents.md)
- [Bounded regression-test generation tutorial](docs/tutorials/regression-tests.md)
- [Bounded transactional patching tutorial](docs/tutorials/bounded-patching.md)
- [Git publication and controlled restart tutorial](docs/tutorials/git-worktrees.md)
- [End-to-end maintenance tutorial](docs/tutorials/end-to-end-maintenance.md)
- [Planned lightweight SQLite viewer](docs/plans/sqlite-viewer.md)

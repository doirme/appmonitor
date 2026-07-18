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

See [the initial plan](docs/initial-plan.md) and [API documentation](docs/api/index.md).


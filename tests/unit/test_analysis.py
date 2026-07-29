"""Tests for deterministic static repository analysis."""

from __future__ import annotations

from typing import TYPE_CHECKING

from appmonitor.analysis import StaticAnalyzer, SymbolKind, ToolStatus
from appmonitor.repository import CommandResult

if TYPE_CHECKING:
    from pathlib import Path


class AnalysisRunner:
    """Return configured results for the fixed analysis command allowlist."""

    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        """Store command results and initialize the call log."""
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, command: tuple[str, ...], *, cwd: Path) -> CommandResult:
        """Record a command and return its configured result."""
        del cwd
        self.calls.append(command)
        return self.results[command]


def test_analyzer_indexes_python_symbols_imports_and_docstrings(tmp_path: Path) -> None:
    """AST indexing extracts compact structural context without importing target code."""
    source = tmp_path / "src" / "sample.py"
    source.parent.mkdir()
    source.write_text(
        '"""Sample module."""\n'
        "import json\n"
        "from pathlib import Path\n\n"
        "class Worker:\n"
        '    """Perform work."""\n\n'
        "    def run(self, value: int = 1) -> str:\n"
        '        """Return a formatted value."""\n'
        "        return str(value)\n",
        encoding="utf-8",
    )

    report = StaticAnalyzer(run_tools=False).analyze(tmp_path)

    assert report.syntax_errors == ()
    assert {item.name for item in report.imports} == {"json", "pathlib.Path"}
    symbols = {symbol.qualified_name: symbol for symbol in report.symbols}
    assert symbols["Worker"].kind is SymbolKind.CLASS
    assert symbols["Worker"].docstring == "Perform work."
    assert symbols["Worker.run"].kind is SymbolKind.FUNCTION
    assert symbols["Worker.run"].signature == "self, value: int=1"
    assert symbols["Worker.run"].returns == "str"


def test_analyzer_records_syntax_errors_without_stopping_other_files(tmp_path: Path) -> None:
    """An invalid module becomes a finding while valid modules remain indexed."""
    (tmp_path / "valid.py").write_text("def valid():\n    return 1\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    cache = tmp_path / ".uv-cache"
    cache.mkdir()
    (cache / "ignored.py").write_text("def ignored():\n    return 1\n", encoding="utf-8")

    report = StaticAnalyzer(run_tools=False).analyze(tmp_path)

    assert {symbol.qualified_name for symbol in report.symbols} == {"valid"}
    assert len(report.syntax_errors) == 1
    assert report.syntax_errors[0].path == "broken.py"
    assert report.syntax_errors[0].line == 1


def test_analyzer_runs_only_fixed_quality_commands(tmp_path: Path) -> None:
    """Tool execution is constrained to the declared deterministic allowlist."""
    commands = StaticAnalyzer.tool_commands()
    runner = AnalysisRunner(
        {
            command: CommandResult(
                127 if name == "coverage" else (1 if name == "mypy" else 0),
                "output",
                "error",
            )
            for name, command in commands
        },
    )

    report = StaticAnalyzer(runner=runner).analyze(tmp_path)

    assert runner.calls == [command for _, command in commands]
    statuses = {tool.name: tool.status for tool in report.tools}
    assert statuses["ruff"] is ToolStatus.PASSED
    assert statuses["mypy"] is ToolStatus.FAILED
    assert statuses["coverage"] is ToolStatus.UNAVAILABLE

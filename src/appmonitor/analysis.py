"""Deterministic AST indexing and fixed quality-tool analysis."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from appmonitor.repository import CommandRunner, SubprocessCommandRunner

if TYPE_CHECKING:
    from pathlib import Path

_IGNORED_DIRECTORIES = frozenset(
    {
        ".appmonitor",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    },
)
_COMMAND_NOT_FOUND_EXIT_CODE = 127


class SymbolKind(StrEnum):
    """Kinds of Python symbols included in the local index."""

    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async_function"


class ToolStatus(StrEnum):
    """Normalized deterministic quality-tool outcomes."""

    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """Compact structural record for a Python class or function."""

    path: str
    module: str
    qualified_name: str
    name: str
    kind: SymbolKind
    line: int
    signature: str | None
    returns: str | None
    docstring: str | None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible symbol payload."""
        return {
            "path": self.path,
            "module": self.module,
            "qualified_name": self.qualified_name,
            "name": self.name,
            "kind": self.kind.value,
            "line": self.line,
            "signature": self.signature,
            "returns": self.returns,
            "docstring": self.docstring,
        }


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One imported module or symbol found through AST parsing."""

    path: str
    module: str
    name: str
    line: int

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible import payload."""
        return {"path": self.path, "module": self.module, "name": self.name, "line": self.line}


@dataclass(frozen=True, slots=True)
class SyntaxFinding:
    """Syntax error found while parsing a target module."""

    path: str
    line: int | None
    column: int | None
    message: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible syntax-error payload."""
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ToolCheck:
    """Result of one allowlisted deterministic quality command."""

    name: str
    command: tuple[str, ...]
    status: ToolStatus
    exit_code: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible tool payload."""
        return {
            "name": self.name,
            "command": list(self.command),
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True, slots=True)
class StaticAnalysisReport:
    """Complete deterministic static-analysis result for one repository."""

    symbols: tuple[SymbolRecord, ...] = ()
    imports: tuple[ImportRecord, ...] = ()
    syntax_errors: tuple[SyntaxFinding, ...] = ()
    tools: tuple[ToolCheck, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible report payload."""
        return {
            "symbols": [symbol.to_dict() for symbol in self.symbols],
            "imports": [item.to_dict() for item in self.imports],
            "syntax_errors": [finding.to_dict() for finding in self.syntax_errors],
            "tools": [tool.to_dict() for tool in self.tools],
        }


class StaticAnalyzer:
    """Index Python source and execute a fixed deterministic quality suite."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        run_tools: bool = True,
    ) -> None:
        """Create an analyzer with an injectable infrastructure boundary."""
        self._runner = runner or SubprocessCommandRunner()
        self._run_tools = run_tools

    @staticmethod
    def tool_commands() -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Return the immutable allowlist of deterministic quality commands."""
        return (
            ("ruff", ("uv", "run", "ruff", "check", ".", "--output-format", "json")),
            ("mypy", ("uv", "run", "mypy", ".")),
            ("compile", ("uv", "run", "python", "-m", "compileall", "-q", ".")),
            ("pytest-collect", ("uv", "run", "pytest", "--collect-only", "-q")),
            (
                "coverage",
                ("uv", "run", "pytest", "--cov", "--cov-branch", "--cov-report=json", "-q"),
            ),
        )

    def analyze(self, repository: Path) -> StaticAnalysisReport:
        """Analyze Python files and optionally execute the fixed tool suite."""
        root = repository.resolve()
        symbols: list[SymbolRecord] = []
        imports: list[ImportRecord] = []
        syntax_errors: list[SyntaxFinding] = []
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root)
            if any(part in _IGNORED_DIRECTORIES for part in relative.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
            except (SyntaxError, UnicodeDecodeError) as error:
                syntax_errors.append(_syntax_finding(relative, error))
                continue
            visitor = _IndexVisitor(relative)
            visitor.visit(tree)
            symbols.extend(visitor.symbols)
            imports.extend(visitor.imports)
        tools = self._execute_tools(root) if self._run_tools else ()
        return StaticAnalysisReport(
            symbols=tuple(symbols),
            imports=tuple(imports),
            syntax_errors=tuple(syntax_errors),
            tools=tools,
        )

    def _execute_tools(self, root: Path) -> tuple[ToolCheck, ...]:
        """Execute every allowlisted tool command in declaration order."""
        checks: list[ToolCheck] = []
        for name, command in self.tool_commands():
            result = self._runner.run(command, cwd=root)
            if result.exit_code == _COMMAND_NOT_FOUND_EXIT_CODE:
                status = ToolStatus.UNAVAILABLE
            elif result.exit_code == 0:
                status = ToolStatus.PASSED
            else:
                status = ToolStatus.FAILED
            checks.append(
                ToolCheck(
                    name=name,
                    command=command,
                    status=status,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                ),
            )
        return tuple(checks)


class _IndexVisitor(ast.NodeVisitor):
    """Collect compact symbols and imports from one parsed module."""

    def __init__(self, relative_path: Path) -> None:
        self.path = relative_path.as_posix()
        self.module = relative_path.with_suffix("").as_posix().replace("/", ".")
        self.parents: list[str] = []
        self.symbols: list[SymbolRecord] = []
        self.imports: list[ImportRecord] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Record a class and index its nested symbols."""
        self._record_symbol(node, SymbolKind.CLASS)
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Record a synchronous function and its nested symbols."""
        self._visit_function(node, SymbolKind.FUNCTION)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Record an asynchronous function and its nested symbols."""
        self._visit_function(node, SymbolKind.ASYNC_FUNCTION)

    def visit_Import(self, node: ast.Import) -> None:
        """Record direct imports."""
        self.imports.extend(
            ImportRecord(self.path, self.module, alias.name, node.lineno) for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record imported names with their source module."""
        prefix = "." * node.level + (node.module or "")
        self.imports.extend(
            ImportRecord(self.path, self.module, f"{prefix}.{alias.name}".strip("."), node.lineno)
            for alias in node.names
        )

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        kind: SymbolKind,
    ) -> None:
        """Record a function and traverse nested definitions."""
        self._record_symbol(node, kind)
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    def _record_symbol(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: SymbolKind,
    ) -> None:
        """Append one normalized symbol record."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = ast.unparse(node.args)
            returns = ast.unparse(node.returns) if node.returns else None
        else:
            signature = None
            returns = None
        self.symbols.append(
            SymbolRecord(
                path=self.path,
                module=self.module,
                qualified_name=".".join([*self.parents, node.name]),
                name=node.name,
                kind=kind,
                line=node.lineno,
                signature=signature,
                returns=returns,
                docstring=ast.get_docstring(node),
            ),
        )


def _syntax_finding(relative: Path, error: SyntaxError | UnicodeDecodeError) -> SyntaxFinding:
    """Normalize parser and decoding errors."""
    if isinstance(error, SyntaxError):
        return SyntaxFinding(relative.as_posix(), error.lineno, error.offset, error.msg)
    return SyntaxFinding(relative.as_posix(), None, None, str(error))

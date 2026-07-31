"""Read-only reporting projections for AppMonitor SQLite databases."""

# ruff: noqa: S608

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

_SQLITE_HEADER = b"SQLite format 3\x00"
_MAX_PAGE_SIZE = 1_000
_TABLES = (
    "runs",
    "log_lines",
    "metrics",
    "artifacts",
    "run_states",
    "run_contexts",
    "run_analyses",
    "run_goals",
    "instrumented_calls",
    "llm_calls",
    "run_diagnostics",
    "run_regression_tests",
    "run_patches",
    "run_git_maintenance",
)
_RUN_COLUMNS = {
    "run_id",
    "repository",
    "command_json",
    "outcome",
    "exit_code",
    "timed_out",
    "started_at",
    "finished_at",
    "report_json",
}
type SQLiteValue = bytes | float | int | str | None


class RunDetail(TypedDict):
    """Portable report payload selected by run identifier."""

    run_id: str
    report: dict[str, object]


class ReportDatabaseError(ValueError):
    """Indicate that a file is not a readable AppMonitor database."""


@dataclass(frozen=True, slots=True)
class OverviewStats:
    """Top-level operational counters."""

    runs: int
    succeeded: int
    failed: int
    timed_out: int
    latest_activity: str | None
    llm_calls: int
    llm_cost_usd: float
    llm_success_rate: float | None
    average_llm_latency_seconds: float | None
    accepted_patches: int
    rejected_patches: int
    pushed_branches: int
    restarts: int
    stop_decisions: int


@dataclass(frozen=True, slots=True)
class ReportPage:
    """One immutable page of tabular report rows."""

    columns: tuple[str, ...]
    rows: tuple[dict[str, SQLiteValue], ...]
    total: int
    limit: int
    offset: int


class ReportDatabase:
    """Query one AppMonitor database without migrations or write access."""

    def __init__(self, database: str | Path, *, busy_timeout_ms: int = 2_000) -> None:
        """Validate a SQLite file and retain bounded read settings."""
        self.database = Path(database).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        if busy_timeout_ms < 0:
            message = "busy_timeout_ms cannot be negative"
            raise ValueError(message)
        self._validate()

    def connect(self) -> sqlite3.Connection:
        """Open a read-only, query-only connection."""
        uri = f"{self.database.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA query_only = ON")
        return connection

    def overview(self) -> OverviewStats:
        """Return the principal run, LLM, patch, Git, and recovery totals."""
        with closing(self.connect()) as connection:
            run = connection.execute(
                """
                SELECT COUNT(*) AS runs,
                       COALESCE(SUM(outcome = 'succeeded'), 0) AS succeeded,
                       COALESCE(SUM(outcome = 'failed'), 0) AS failed,
                       COALESCE(SUM(timed_out = 1 OR outcome = 'timed_out'), 0) AS timed_out,
                       MAX(finished_at) AS latest_activity
                FROM runs
                """
            ).fetchone()
            llm = self._llm_overview(connection)
            patches = self._status_counts(connection, "run_patches")
            git = self._git_overview(connection)
        return OverviewStats(
            runs=int(_required_value(run["runs"])),
            succeeded=int(_required_value(run["succeeded"])),
            failed=int(_required_value(run["failed"])),
            timed_out=int(_required_value(run["timed_out"])),
            latest_activity=run["latest_activity"],
            llm_calls=int(_required_value(llm["calls"])),
            llm_cost_usd=float(_required_value(llm["cost"])),
            llm_success_rate=_optional_float(llm["success_rate"]),
            average_llm_latency_seconds=_optional_float(llm["latency"]),
            accepted_patches=patches.get("accepted", 0),
            rejected_patches=patches.get("rejected", 0),
            pushed_branches=git["pushed"],
            restarts=git["restarts"],
            stop_decisions=git["stops"],
        )

    def runs(
        self,
        *,
        outcome: str | None = None,
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> ReportPage:
        """List runs ordered by recent activity with optional outcome and text filters."""
        _validate_page(limit, offset)
        clauses: list[str] = []
        parameters: list[object] = []
        if outcome:
            clauses.append("outcome = ?")
            parameters.append(outcome)
        if search:
            clauses.append("(run_id LIKE ? OR repository LIKE ? OR command_json LIKE ?)")
            pattern = f"%{search}%"
            parameters.extend((pattern, pattern, pattern))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        select = f"""
            SELECT run_id, repository, command_json, outcome, exit_code, timed_out,
                   started_at, finished_at,
                   ROUND((julianday(finished_at) - julianday(started_at)) * 86400, 3)
                       AS duration_seconds
            FROM runs{where}
            ORDER BY finished_at DESC, run_id
        """
        count = f"SELECT COUNT(*) FROM runs{where}"
        return self._page(select, count, parameters, limit, offset)

    def run_detail(self, run_id: str) -> RunDetail:
        """Load the persisted portable report for one run."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT report_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        try:
            payload = json.loads(row["report_json"])
        except (json.JSONDecodeError, TypeError) as error:
            message = f"run {run_id!r} has malformed report JSON"
            raise ReportDatabaseError(message) from error
        if not isinstance(payload, dict):
            message = f"run {run_id!r} report JSON is not an object"
            raise ReportDatabaseError(message)
        return {"run_id": run_id, "report": cast("dict[str, object]", payload)}

    def runtime_metrics(
        self,
        run_id: str | None = None,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> ReportPage:
        """Return metric samples for all runs or one selected run."""
        if not self.has_table("metrics"):
            return _empty_page(limit, offset)
        _validate_page(limit, offset)
        where = " WHERE run_id = ?" if run_id else ""
        parameters: list[object] = [run_id] if run_id else []
        select = f"""
            SELECT run_id, sequence, timestamp, rss_bytes, cpu_percent,
                   process_count, thread_count
            FROM metrics{where}
            ORDER BY timestamp DESC, run_id, sequence
        """
        return self._page(
            select,
            f"SELECT COUNT(*) FROM metrics{where}",
            parameters,
            limit,
            offset,
        )

    def llm_stats(self, *, limit: int = 100, offset: int = 0) -> ReportPage:
        """Aggregate model reliability, latency, token use, and cost by task."""
        required = {
            "task",
            "model",
            "status",
            "latency_seconds",
            "prompt_tokens",
            "completion_tokens",
            "cost_usd",
        }
        if not self._has_columns("llm_calls", required):
            return _empty_page(limit, offset)
        select = """
            SELECT task, model, COUNT(*) AS calls,
                   ROUND(AVG(status = 'succeeded'), 4) AS success_rate,
                   SUM(status = 'invalid_response') AS invalid_responses,
                   SUM(status = 'provider_error') AS provider_errors,
                   ROUND(AVG(latency_seconds), 4) AS average_latency_seconds,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   ROUND(SUM(cost_usd), 8) AS cost_usd
            FROM llm_calls
            GROUP BY task, model
            ORDER BY cost_usd DESC, calls DESC, task, model
        """
        count = "SELECT COUNT(*) FROM (SELECT 1 FROM llm_calls GROUP BY task, model)"
        return self._page(select, count, [], limit, offset)

    def maintenance(self, *, limit: int = 100, offset: int = 0) -> ReportPage:
        """List patch decisions joined to their source run."""
        required = {"run_id", "status", "reason", "patch_sha256"}
        if not self._has_columns("run_patches", required):
            return _empty_page(limit, offset)
        select = """
            SELECT p.run_id, r.finished_at, r.outcome AS run_outcome,
                   p.status AS patch_status, p.reason, p.patch_sha256
            FROM run_patches AS p
            LEFT JOIN runs AS r ON r.run_id = p.run_id
            ORDER BY r.finished_at DESC, p.run_id
        """
        return self._page(select, "SELECT COUNT(*) FROM run_patches", [], limit, offset)

    def git_recovery(self, *, limit: int = 100, offset: int = 0) -> ReportPage:
        """List dedicated branches, pushes, and restart or stop decisions."""
        required = {"run_id", "status", "branch", "base_commit"}
        if not self._has_columns("run_git_maintenance", required):
            return _empty_page(limit, offset)
        available = self.columns("run_git_maintenance")
        optional = (
            "commit_sha",
            "remote_name",
            "pushed",
            "restart_action",
            "restart_run_id",
            "restart_outcome",
        )
        projections = [name if name in available else f"NULL AS {name}" for name in optional]
        select = f"""
            SELECT run_id, status, branch, base_commit, {", ".join(projections)}
            FROM run_git_maintenance
            ORDER BY rowid DESC
        """
        return self._page(
            select,
            "SELECT COUNT(*) FROM run_git_maintenance",
            [],
            limit,
            offset,
        )

    def table(
        self,
        name: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> ReportPage:
        """Browse one allow-listed raw table with bounded pagination."""
        if name not in self.tables():
            message = f"table {name!r} is not available"
            raise ValueError(message)
        return self._page(
            f"SELECT * FROM {name} ORDER BY rowid DESC",
            f"SELECT COUNT(*) FROM {name}",
            [],
            limit,
            offset,
        )

    def tables(self) -> tuple[str, ...]:
        """Return known AppMonitor tables present in this database."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        present = {str(row["name"]) for row in rows}
        return tuple(name for name in _TABLES if name in present)

    def has_table(self, name: str) -> bool:
        """Return whether one known table exists."""
        return name in _TABLES and name in self.tables()

    def columns(self, table: str) -> frozenset[str]:
        """Return column names for one known existing table."""
        if table not in _TABLES:
            return frozenset()
        with closing(self.connect()) as connection:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return frozenset(str(row["name"]) for row in rows)

    def _validate(self) -> None:
        """Verify the file header and minimum AppMonitor schema."""
        try:
            with self.database.open("rb") as stream:
                header = stream.read(len(_SQLITE_HEADER))
        except OSError as error:
            message = f"cannot read database: {self.database}"
            raise ReportDatabaseError(message) from error
        if header != _SQLITE_HEADER:
            message = f"not a SQLite database: {self.database}"
            raise ReportDatabaseError(message)
        try:
            columns = self.columns("runs")
        except sqlite3.Error as error:
            message = f"cannot inspect database: {self.database}"
            raise ReportDatabaseError(message) from error
        missing = _RUN_COLUMNS - columns
        if missing:
            message = f"invalid AppMonitor runs table; missing columns: {sorted(missing)}"
            raise ReportDatabaseError(message)

    def _has_columns(self, table: str, required: set[str]) -> bool:
        """Return whether an optional table has the required known shape."""
        return required.issubset(self.columns(table))

    def _page(
        self,
        select: str,
        count: str,
        parameters: list[object],
        limit: int,
        offset: int,
    ) -> ReportPage:
        """Execute a parameterized projection and count its full result."""
        _validate_page(limit, offset)
        with closing(self.connect()) as connection:
            total = int(connection.execute(count, parameters).fetchone()[0])
            cursor = connection.execute(
                f"{select} LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            )
            columns = tuple(item[0] for item in (cursor.description or ()))
            rows = tuple(dict(zip(columns, row, strict=True)) for row in cursor.fetchall())
        return ReportPage(columns=columns, rows=rows, total=total, limit=limit, offset=offset)

    def _llm_overview(self, connection: sqlite3.Connection) -> dict[str, SQLiteValue]:
        """Aggregate LLM counters when the optional table has a valid shape."""
        required = {"status", "cost_usd", "latency_seconds"}
        if not self._has_columns("llm_calls", required):
            return {"calls": 0, "cost": 0.0, "success_rate": None, "latency": None}
        row = connection.execute(
            """
            SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost,
                   AVG(status = 'succeeded') AS success_rate,
                   AVG(latency_seconds) AS latency
            FROM llm_calls
            """
        ).fetchone()
        return dict(row)

    def _status_counts(self, connection: sqlite3.Connection, table: str) -> dict[str, int]:
        """Count status values for one optional compatible table."""
        if not self._has_columns(table, {"status"}):
            return {}
        rows = connection.execute(
            f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _git_overview(self, connection: sqlite3.Connection) -> dict[str, int]:
        """Aggregate optional Git and recovery fields across schema versions."""
        columns = self.columns("run_git_maintenance")
        if not {"run_id", "status"}.issubset(columns):
            return {"pushed": 0, "restarts": 0, "stops": 0}
        pushed = "COALESCE(SUM(pushed = 1), 0)" if "pushed" in columns else "0"
        restarts = (
            "COALESCE(SUM(restart_action = 'restart'), 0)" if "restart_action" in columns else "0"
        )
        stops = "COALESCE(SUM(restart_action = 'stop'), 0)" if "restart_action" in columns else "0"
        row = connection.execute(
            f"SELECT {pushed} AS pushed, {restarts} AS restarts, {stops} AS stops "
            "FROM run_git_maintenance"
        ).fetchone()
        return {name: int(row[name]) for name in ("pushed", "restarts", "stops")}


def _validate_page(limit: int, offset: int) -> None:
    """Reject unbounded or nonsensical pagination."""
    if not 1 <= limit <= _MAX_PAGE_SIZE:
        message = f"limit must be between 1 and {_MAX_PAGE_SIZE}"
        raise ValueError(message)
    if offset < 0:
        message = "offset cannot be negative"
        raise ValueError(message)


def _empty_page(limit: int, offset: int) -> ReportPage:
    """Return a validated empty projection."""
    _validate_page(limit, offset)
    return ReportPage(columns=(), rows=(), total=0, limit=limit, offset=offset)


def _optional_float(value: SQLiteValue) -> float | None:
    """Convert a nullable SQLite aggregate to a float."""
    return None if value is None else float(value)


def _required_value(value: SQLiteValue) -> bytes | float | int | str:
    """Narrow a value produced by a non-null SQL aggregate."""
    if value is None:
        message = "required SQLite aggregate returned null"
        raise ReportDatabaseError(message)
    return value

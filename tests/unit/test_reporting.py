"""Tests for read-only SQLite reporting projections."""

# ruff: noqa: PLR2004

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from appmonitor.reporting import ReportDatabase, ReportDatabaseError

if TYPE_CHECKING:
    from pathlib import Path


def _database(path: Path) -> Path:
    """Create a representative AppMonitor database without invoking monitored processes."""
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                command_json TEXT NOT NULL,
                outcome TEXT NOT NULL,
                exit_code INTEGER,
                timed_out INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                report_json TEXT NOT NULL
            );
            CREATE TABLE metrics (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                rss_bytes INTEGER NOT NULL,
                cpu_percent REAL NOT NULL,
                process_count INTEGER NOT NULL,
                thread_count INTEGER NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE TABLE llm_calls (
                call_id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                latency_seconds REAL NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                prompt_sha256 TEXT NOT NULL,
                error_type TEXT
            );
            CREATE TABLE run_patches (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                patch_sha256 TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                diff_text TEXT NOT NULL,
                validation_json TEXT NOT NULL,
                review_json TEXT
            );
            CREATE TABLE run_git_maintenance (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                branch TEXT NOT NULL,
                base_commit TEXT NOT NULL,
                commit_sha TEXT,
                changed_paths_json TEXT NOT NULL,
                remote_name TEXT,
                pushed INTEGER NOT NULL,
                restart_action TEXT,
                restart_run_id TEXT,
                restart_outcome TEXT,
                result_json TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "run-ok",
                    "/repo",
                    '["python", "ok.py"]',
                    "succeeded",
                    0,
                    0,
                    "2026-07-30T10:00:00+00:00",
                    "2026-07-30T10:00:02+00:00",
                    '{"stdout": [], "stderr": []}',
                ),
                (
                    "run-fail",
                    "/repo",
                    '["python", "bad.py"]',
                    "failed",
                    1,
                    0,
                    "2026-07-31T10:00:00+00:00",
                    "2026-07-31T10:00:04+00:00",
                    '{"stdout": [], "stderr": [{"message": "boom"}]}',
                ),
                (
                    "run-timeout",
                    "/other",
                    '["python", "slow.py"]',
                    "timed_out",
                    None,
                    1,
                    "2026-07-31T11:00:00+00:00",
                    "2026-07-31T11:00:05+00:00",
                    '{"stdout": [], "stderr": []}',
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ("run-fail", 0, "2026-07-31T10:00:01+00:00", 1024, 10.0, 1, 2),
                ("run-fail", 1, "2026-07-31T10:00:02+00:00", 4096, 20.0, 2, 4),
            ),
        )
        connection.executemany(
            "INSERT INTO llm_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "call-1",
                    "assessment",
                    "model/a",
                    "succeeded",
                    "2026-07-31T10:01:00+00:00",
                    2.0,
                    100,
                    20,
                    0.01,
                    "hash-1",
                    None,
                ),
                (
                    "call-2",
                    "assessment",
                    "model/a",
                    "invalid_response",
                    "2026-07-31T10:02:00+00:00",
                    4.0,
                    120,
                    0,
                    0.02,
                    "hash-2",
                    "schema",
                ),
            ),
        )
        connection.execute(
            "INSERT INTO run_patches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-fail", "accepted", "verified", "sha", "{}", "diff", "{}", "{}"),
        )
        connection.execute(
            "INSERT INTO run_git_maintenance VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-fail",
                "pushed",
                "verified",
                "appmonitor/run-fail",
                "base",
                "commit",
                "[]",
                "origin",
                1,
                "restart",
                "run-ok",
                "succeeded",
                "{}",
            ),
        )
    return path


def test_overview_and_task_statistics_are_aggregated(tmp_path: Path) -> None:
    """Overview and LLM projections expose useful operational totals."""
    report = ReportDatabase(_database(tmp_path / "runs.sqlite3"))

    overview = report.overview()
    llm = report.llm_stats()

    assert overview.runs == 3
    assert overview.succeeded == 1
    assert overview.failed == 1
    assert overview.timed_out == 1
    assert overview.llm_calls == 2
    assert overview.llm_cost_usd == pytest.approx(0.03)
    assert overview.accepted_patches == 1
    assert overview.pushed_branches == 1
    assert overview.restarts == 1
    assert llm.rows[0]["success_rate"] == 0.5
    assert llm.rows[0]["average_latency_seconds"] == 3.0


def test_run_and_runtime_pages_filter_and_bound_results(tmp_path: Path) -> None:
    """Run searches and metric timelines remain ordered and paginated."""
    report = ReportDatabase(_database(tmp_path / "runs.sqlite3"))

    runs = report.runs(outcome="failed", search="bad.py", limit=1, offset=0)
    metrics = report.runtime_metrics("run-fail", limit=1, offset=1)
    detail = report.run_detail("run-fail")

    assert runs.total == 1
    assert runs.rows[0]["run_id"] == "run-fail"
    assert runs.rows[0]["duration_seconds"] == 4.0
    assert metrics.total == 2
    assert metrics.rows[0]["rss_bytes"] == 1024
    stderr = detail["report"]["stderr"]
    assert isinstance(stderr, list)
    assert isinstance(stderr[0], dict)
    assert stderr[0]["message"] == "boom"
    with pytest.raises(ValueError, match="limit"):
        report.runs(limit=0)


def test_maintenance_git_and_raw_table_views_are_read_only(tmp_path: Path) -> None:
    """Maintenance joins and allow-listed table browsing cannot mutate SQLite."""
    database = _database(tmp_path / "runs.sqlite3")
    report = ReportDatabase(database)

    maintenance = report.maintenance()
    git = report.git_recovery()
    raw = report.table("runs", limit=2)

    assert maintenance.rows[0]["patch_status"] == "accepted"
    assert git.rows[0]["remote_name"] == "origin"
    assert raw.total == 3
    assert len(raw.rows) == 2
    with pytest.raises(ValueError, match="not available"):
        report.table("sqlite_master")
    with pytest.raises(sqlite3.OperationalError), report.connect() as connection:
        connection.execute("DELETE FROM runs")


def test_partial_database_has_explicit_empty_optional_projections(tmp_path: Path) -> None:
    """A valid older database may omit all optional feature tables."""
    database = tmp_path / "partial.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, repository TEXT, command_json TEXT,
                outcome TEXT, exit_code INTEGER, timed_out INTEGER,
                started_at TEXT, finished_at TEXT, report_json TEXT
            )
            """
        )

    report = ReportDatabase(database)

    assert report.overview().llm_calls == 0
    assert report.llm_stats().rows == ()
    assert report.maintenance().rows == ()
    assert report.git_recovery().rows == ()


def test_reporting_rejects_invalid_arguments_and_report_payloads(tmp_path: Path) -> None:
    """Invalid configuration, pagination, identifiers, and JSON fail explicitly."""
    database = _database(tmp_path / "runs.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE runs SET report_json = '[]' WHERE run_id = 'run-ok'")
        connection.execute("UPDATE runs SET report_json = '{' WHERE run_id = 'run-fail'")

    report = ReportDatabase(database)

    with pytest.raises(KeyError):
        report.run_detail("missing")
    with pytest.raises(ReportDatabaseError, match="not an object"):
        report.run_detail("run-ok")
    with pytest.raises(ReportDatabaseError, match="malformed"):
        report.run_detail("run-fail")
    with pytest.raises(ValueError, match="offset"):
        report.runs(offset=-1)
    with pytest.raises(ValueError, match="busy_timeout"):
        ReportDatabase(database, busy_timeout_ms=-1)
    assert report.columns("not-an-appmonitor-table") == frozenset()


def test_sqlite_file_without_appmonitor_schema_is_rejected(tmp_path: Path) -> None:
    """A generic SQLite database is not mistaken for AppMonitor persistence."""
    database = tmp_path / "generic.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(ReportDatabaseError, match="runs table"):
        ReportDatabase(database)


@pytest.mark.parametrize("contents", [b"", b"not sqlite"])
def test_invalid_database_is_rejected(tmp_path: Path, contents: bytes) -> None:
    """Missing SQLite structure fails before any view is queried."""
    database = tmp_path / "invalid.sqlite3"
    database.write_bytes(contents)

    with pytest.raises(ReportDatabaseError):
        ReportDatabase(database)

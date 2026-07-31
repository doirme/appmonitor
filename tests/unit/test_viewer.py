"""Tests for the optional Streamlit viewer."""

# ruff: noqa: SLF001

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

import appmonitor.viewer
from appmonitor.reporting import ReportDatabaseError

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest


def _viewer_database(path: Path) -> Path:
    """Create the smallest database accepted by the viewer."""
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, repository TEXT, command_json TEXT,
                outcome TEXT, exit_code INTEGER, timed_out INTEGER,
                started_at TEXT, finished_at TEXT, report_json TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                "/repo",
                '["python", "main.py"]',
                "succeeded",
                0,
                0,
                "2026-07-31T10:00:00+00:00",
                "2026-07-31T10:00:01+00:00",
                '{"stdout": [], "stderr": []}',
            ),
        )
        connection.execute(
            """
            CREATE TABLE metrics (
                run_id TEXT, sequence INTEGER, timestamp TEXT, rss_bytes INTEGER,
                cpu_percent REAL, process_count INTEGER, thread_count INTEGER,
                PRIMARY KEY (run_id, sequence)
            )
            """
        )
        connection.execute(
            "INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("run-1", 0, "2026-07-31T10:00:00+00:00", 1048576, 12.5, 1, 2),
        )
    return path


def test_streamlit_app_renders_all_operational_tabs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default UI exposes every principal reporting view."""
    database = _viewer_database(tmp_path / "runs.sqlite3")
    monkeypatch.setenv("APPMONITOR_VIEWER_DATABASE", str(database))

    app = AppTest.from_file(str(Path(appmonitor.viewer.__file__))).run(timeout=10)

    assert not app.exception
    expected_tabs = {
        "Overview",
        "Runs",
        "Runtime",
        "LLM",
        "Maintenance",
        "Git and recovery",
        "Tables",
    }
    assert expected_tabs.issubset({tab.label for tab in app.tabs})
    assert any(metric.label == "Runs" and metric.value == "1" for metric in app.metric)
    assert any(metric.label == "Peak RSS" and metric.value == "1.0 MiB" for metric in app.metric)


def test_launcher_passes_database_to_streamlit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console entry point delegates execution to Streamlit with a fixed script."""
    database = tmp_path / "runs.sqlite3"
    captured: list[list[str]] = []

    def fake_streamlit(arguments: list[str]) -> int:
        captured.append(arguments)
        return 0

    monkeypatch.setattr(appmonitor.viewer, "_run_streamlit", fake_streamlit)

    assert appmonitor.viewer.main(["--database", str(database)]) == 0
    assert captured[0][-2:] == ["--database", str(database.resolve())]


def test_viewer_error_state_and_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid databases and defensive helper branches remain user-visible and bounded."""
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_text("bad", encoding="utf-8")
    valid = _viewer_database(tmp_path / "valid.sqlite3")
    monkeypatch.setenv("APPMONITOR_VIEWER_DATABASE", str(invalid))

    app = AppTest.from_file(str(Path(appmonitor.viewer.__file__))).run(timeout=10)

    assert app.error
    assert appmonitor.viewer._without_large_sections("text") == "text"
    assert appmonitor.viewer._line_messages("text", "stdout") == ""
    assert appmonitor.viewer._line_messages({"stdout": "text"}, "stdout") == ""
    assert appmonitor.viewer._mapping_value("text", "key", "default") == "default"
    with pytest.raises(ValueError, match="unknown viewer operation"):
        appmonitor.viewer._query(str(valid), "unknown", "{}")
    with pytest.raises(ReportDatabaseError, match="required report cell is null"):
        appmonitor.viewer._required_cell(None)


def test_streamlit_runner_restores_process_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real launcher adapter does not leak Streamlit arguments to callers."""
    import streamlit.web.cli  # noqa: PLC0415

    original = sys.argv
    observed: list[str] = []

    def fake_main() -> int:
        observed.extend(sys.argv)
        return 0

    monkeypatch.setattr(streamlit.web.cli, "main", fake_main)

    assert appmonitor.viewer._run_streamlit(["run", "viewer.py"]) == 0
    assert observed == ["streamlit", "run", "viewer.py"]
    assert sys.argv is original

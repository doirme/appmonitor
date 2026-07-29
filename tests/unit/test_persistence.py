"""Tests for transactional SQLite run persistence."""

import sqlite3
import sys
from pathlib import Path

import pytest

from appmonitor import RunSpec
from appmonitor.execution import LocalExecutor
from appmonitor.persistence import SQLiteRunStore


def test_store_persists_normalized_run_data(tmp_path: Path) -> None:
    """A stored report remains queryable after reopening the database."""
    target = tmp_path / "target.py"
    target.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "print('saved stdout')\n"
        "print('saved stderr', file=sys.stderr)\n"
        "Path('artifact.txt').write_text('data', encoding='utf-8')\n",
        encoding="utf-8",
    )
    report = LocalExecutor().execute(
        RunSpec(repository=tmp_path, command=[sys.executable, str(target)]),
    )
    database = tmp_path / "runs.sqlite3"

    run_id = SQLiteRunStore(database).save(report)
    stored = SQLiteRunStore(database).load(run_id)

    assert stored["run_id"] == run_id
    assert stored["outcome"] == "succeeded"
    assert stored["stdout"][0]["message"] == "saved stdout"
    assert stored["stderr"][0]["message"] == "saved stderr"
    assert stored["metrics"]
    assert stored["artifacts"]["created"][0]["path"] == "artifact.txt"


def test_store_rejects_duplicate_run_id_atomically(tmp_path: Path) -> None:
    """A duplicate identifier cannot append partially duplicated child rows."""
    report = LocalExecutor().execute(
        RunSpec(repository=tmp_path, command=[sys.executable, "-c", "print('once')"]),
    )
    database = tmp_path / "runs.sqlite3"
    store = SQLiteRunStore(database)
    store.save(report, run_id="fixed-id")

    with pytest.raises(sqlite3.IntegrityError):
        store.save(report, run_id="fixed-id")

    with sqlite3.connect(database) as connection:
        run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        line_count = connection.execute("SELECT COUNT(*) FROM log_lines").fetchone()[0]
    assert run_count == 1
    assert line_count == 1


def test_store_reports_unknown_run(tmp_path: Path) -> None:
    """Loading an unknown run fails explicitly."""
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")

    with pytest.raises(KeyError, match="missing"):
        store.load("missing")

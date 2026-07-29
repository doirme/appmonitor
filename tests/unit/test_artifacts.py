"""Tests for filesystem artifact snapshots."""

from pathlib import Path

from appmonitor.artifacts import snapshot_files


def test_snapshot_excludes_environment_secret_files(tmp_path: Path) -> None:
    """Environment files must never become persisted artifact metadata."""
    (tmp_path / ".env").write_text("SECRET=one\n", encoding="utf-8")
    (tmp_path / ".env.txt").write_text("SECRET=two\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("public\n", encoding="utf-8")

    snapshot = snapshot_files(tmp_path)

    assert set(snapshot) == {"visible.txt"}


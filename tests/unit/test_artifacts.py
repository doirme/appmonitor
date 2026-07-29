"""Tests for filesystem artifact snapshots."""

import shutil
import subprocess
from pathlib import Path

from appmonitor.artifacts import snapshot_files


def test_snapshot_excludes_environment_secret_files(tmp_path: Path) -> None:
    """Environment files must never become persisted artifact metadata."""
    (tmp_path / ".env").write_text("SECRET=one\n", encoding="utf-8")
    (tmp_path / ".env.txt").write_text("SECRET=two\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("public\n", encoding="utf-8")

    snapshot = snapshot_files(tmp_path)

    assert set(snapshot) == {"visible.txt"}


def test_snapshot_respects_gitignore_for_repository_files(tmp_path: Path) -> None:
    """Git repositories avoid hashing dependency caches and generated workspaces."""
    git = shutil.which("git")
    if git is None:
        msg = "Git is required by the repository test suite"
        raise RuntimeError(msg)
    subprocess.run((git, "init", "-q"), cwd=tmp_path, check=True)  # noqa: S603
    (tmp_path / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("public\n", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "large.bin").write_bytes(b"ignored")

    snapshot = snapshot_files(tmp_path)

    assert set(snapshot) == {".gitignore", "visible.txt"}

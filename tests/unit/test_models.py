"""Tests for public run models."""

from pathlib import Path

import pytest

from appmonitor import RunSpec


def test_run_spec_resolves_repository_and_normalizes_command(tmp_path: Path) -> None:
    """A run specification stores an absolute repository and immutable command."""
    spec = RunSpec(repository=tmp_path, command=["python", "script.py"])

    assert spec.repository == tmp_path.resolve()
    assert spec.command == ("python", "script.py")
    assert spec.timeout_seconds is None


@pytest.mark.parametrize("command", [[], [""]])
def test_run_spec_rejects_empty_commands(tmp_path: Path, command: list[str]) -> None:
    """An executable command is required."""
    with pytest.raises(ValueError, match="command"):
        RunSpec(repository=tmp_path, command=command)


def test_run_spec_rejects_missing_repository(tmp_path: Path) -> None:
    """Runs cannot start from an unknown working directory."""
    with pytest.raises(ValueError, match="repository"):
        RunSpec(repository=tmp_path / "missing", command=["python"])


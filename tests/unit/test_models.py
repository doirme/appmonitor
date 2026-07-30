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
    assert spec.sync_environment is False
    assert spec.analyze_repository is False


@pytest.mark.parametrize("command", [[], [""]])
def test_run_spec_rejects_empty_commands(tmp_path: Path, command: list[str]) -> None:
    """An executable command is required."""
    with pytest.raises(ValueError, match="command"):
        RunSpec(repository=tmp_path, command=command)


def test_run_spec_rejects_missing_repository(tmp_path: Path) -> None:
    """Runs cannot start from an unknown working directory."""
    with pytest.raises(ValueError, match="repository"):
        RunSpec(repository=tmp_path / "missing", command=["python"])


def test_run_spec_enables_remote_git_only_with_an_explicit_remote(tmp_path: Path) -> None:
    """Remote publication is opt-in and normalizes the configured remote name."""
    local = RunSpec(repository=tmp_path, command=["python"])
    remote = RunSpec(repository=tmp_path, command=["python"], git_remote=" origin ")

    assert local.git_remote is None
    assert remote.git_remote == "origin"


@pytest.mark.parametrize("remote", ["", "bad remote", "../origin", "-unsafe"])
def test_run_spec_rejects_unsafe_git_remote(tmp_path: Path, remote: str) -> None:
    """Remote names cannot inject Git options or ambiguous ref syntax."""
    with pytest.raises(ValueError, match="git_remote"):
        RunSpec(repository=tmp_path, command=["python"], git_remote=remote)

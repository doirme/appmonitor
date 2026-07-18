"""Immutable public input models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Describe one deterministic command execution."""

    repository: Path
    command: tuple[str, ...]
    timeout_seconds: float | None = None
    base_branch: str | None = None
    goal_file: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __init__(  # noqa: PLR0913 - public specification fields remain explicit
        self,
        repository: str | Path,
        command: Sequence[str],
        timeout_seconds: float | None = None,
        base_branch: str | None = None,
        goal_file: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """Validate and normalize run inputs."""
        resolved_repository = Path(repository).resolve()
        normalized_command = tuple(command)
        if not resolved_repository.is_dir():
            msg = f"repository is not a directory: {resolved_repository}"
            raise ValueError(msg)
        if not normalized_command or not normalized_command[0]:
            msg = "command must contain a non-empty executable"
            raise ValueError(msg)
        if timeout_seconds is not None and timeout_seconds <= 0:
            msg = "timeout_seconds must be greater than zero"
            raise ValueError(msg)

        object.__setattr__(self, "repository", resolved_repository)
        object.__setattr__(self, "command", normalized_command)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "base_branch", base_branch)
        object.__setattr__(self, "goal_file", Path(goal_file).resolve() if goal_file else None)
        object.__setattr__(self, "environment", MappingProxyType(dict(environment or {})))

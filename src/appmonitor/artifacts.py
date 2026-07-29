"""Filesystem snapshots and artifact change detection."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_IGNORED_DIRECTORIES = frozenset({".git", ".appmonitor", ".venv", "__pycache__"})
_IGNORED_FILE_PREFIXES = (".env",)


@dataclass(frozen=True, slots=True)
class Artifact:
    """Metadata identifying one repository file version."""

    path: str
    size_bytes: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactChanges:
    """Files created, changed, or deleted during a run."""

    created: tuple[Artifact, ...] = ()
    modified: tuple[Artifact, ...] = ()
    deleted: tuple[Artifact, ...] = ()


def snapshot_files(root: Path) -> dict[str, Artifact]:
    """Build a content-addressed snapshot of regular repository files."""
    snapshot: dict[str, Artifact] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if (
            not path.is_file()
            or any(part in _IGNORED_DIRECTORIES for part in relative.parts)
            or path.name.startswith(_IGNORED_FILE_PREFIXES)
        ):
            continue
        stat = path.stat()
        key = relative.as_posix()
        snapshot[key] = Artifact(
            path=key,
            size_bytes=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            sha256=_hash_file(path),
        )
    return snapshot


def compare_snapshots(
    before: dict[str, Artifact],
    after: dict[str, Artifact],
) -> ArtifactChanges:
    """Compare two snapshots and classify repository file changes."""
    created = tuple(after[path] for path in sorted(after.keys() - before.keys()))
    deleted = tuple(before[path] for path in sorted(before.keys() - after.keys()))
    modified = tuple(
        after[path]
        for path in sorted(before.keys() & after.keys())
        if before[path].sha256 != after[path].sha256
    )
    return ArtifactChanges(created=created, modified=modified, deleted=deleted)


def _hash_file(path: Path) -> str:
    """Return a streaming SHA-256 digest for one file."""
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

"""Transactional SQLite persistence for deterministic run reports."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from appmonitor.artifacts import Artifact
    from appmonitor.execution import CapturedLine, RunReport
    from appmonitor.repository import EnvironmentFacts, RepositoryFacts
    from appmonitor.states import StateTransition


class StoredLine(TypedDict):
    """JSON shape of one persisted output line."""

    timestamp: str
    message: str


class StoredMetric(TypedDict):
    """JSON shape of one persisted metric sample."""

    timestamp: str
    rss_bytes: int
    cpu_percent: float
    process_count: int
    thread_count: int


class StoredArtifact(TypedDict):
    """JSON shape of one persisted artifact."""

    path: str
    size_bytes: int
    modified_ns: int
    sha256: str


class StoredArtifacts(TypedDict):
    """JSON shape of persisted artifact changes."""

    created: list[StoredArtifact]
    modified: list[StoredArtifact]
    deleted: list[StoredArtifact]


class StoredTransition(TypedDict):
    """JSON shape of one persisted lifecycle transition."""

    previous: str
    current: str
    cause: str
    actor: str
    timestamp: str


class StoredRepositoryFacts(TypedDict):
    """JSON shape of persisted repository identity."""

    root: str
    git_available: bool
    is_git_repository: bool
    git_root: str | None
    commit: str | None
    branch: str | None
    dirty: bool | None
    has_pyproject: bool
    has_uv_lock: bool
    uv_lock_sha256: str | None


class StoredEnvironmentFacts(TypedDict):
    """JSON shape of persisted environment preparation."""

    python_executable: str
    uv_sync_performed: bool
    uv_sync_succeeded: bool | None
    uv_sync_command: list[str] | None
    uv_sync_exit_code: int | None
    uv_sync_stdout: str
    uv_sync_stderr: str


class StoredRun(TypedDict):
    """JSON shape returned by the SQLite store."""

    run_id: str
    command: list[str]
    repository: str
    outcome: str
    exit_code: int | None
    timed_out: bool
    started_at: str
    finished_at: str
    stdout: list[StoredLine]
    stderr: list[StoredLine]
    metrics: list[StoredMetric]
    artifacts: StoredArtifacts
    transitions: list[StoredTransition]
    repository_facts: StoredRepositoryFacts
    environment_facts: StoredEnvironmentFacts


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    command_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL CHECK (timed_out IN (0, 1)),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_lines (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    stream TEXT NOT NULL CHECK (stream IN ('stdout', 'stderr')),
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    message TEXT NOT NULL,
    PRIMARY KEY (run_id, stream, sequence)
);
CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    rss_bytes INTEGER NOT NULL,
    cpu_percent REAL NOT NULL,
    process_count INTEGER NOT NULL,
    thread_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    change_kind TEXT NOT NULL CHECK (change_kind IN ('created', 'modified', 'deleted')),
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, change_kind, path)
);
CREATE TABLE IF NOT EXISTS run_states (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    previous_state TEXT NOT NULL,
    current_state TEXT NOT NULL,
    cause TEXT NOT NULL,
    actor TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS run_contexts (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    repository_json TEXT NOT NULL,
    environment_json TEXT NOT NULL
);
"""


class SQLiteRunStore:
    """Persist complete and normalized run reports in a local SQLite database."""

    def __init__(self, database: str | Path) -> None:
        """Create a store and initialize its schema when needed."""
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(_SCHEMA)

    def save(
        self,
        report: RunReport,
        *,
        run_id: str | None = None,
        transitions: Sequence[StateTransition] = (),
        repository_facts: RepositoryFacts | None = None,
        environment_facts: EnvironmentFacts | None = None,
    ) -> str:
        """Save a report atomically and return its durable run identifier."""
        identifier = run_id or str(uuid4())
        payload = cast("StoredRun", json.loads(report.to_json()))
        payload["run_id"] = identifier
        payload["transitions"] = [
            {
                "previous": transition.previous.value,
                "current": transition.current.value,
                "cause": transition.cause,
                "actor": transition.actor,
                "timestamp": transition.timestamp.isoformat(),
            }
            for transition in transitions
        ]
        repository_payload = repository_facts.to_dict() if repository_facts else {}
        environment_payload = environment_facts.to_dict() if environment_facts else {}
        payload["repository_facts"] = cast("StoredRepositoryFacts", repository_payload)
        payload["environment_facts"] = cast("StoredEnvironmentFacts", environment_payload)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, repository, command_json, outcome, exit_code, timed_out,
                    started_at, finished_at, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    report.repository,
                    json.dumps(report.command),
                    report.outcome.value,
                    report.exit_code,
                    report.timed_out,
                    report.started_at.isoformat(),
                    report.finished_at.isoformat(),
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self._save_lines(connection, identifier, "stdout", report.stdout)
            self._save_lines(connection, identifier, "stderr", report.stderr)
            connection.executemany(
                """
                INSERT INTO metrics (
                    run_id, sequence, timestamp, rss_bytes, cpu_percent,
                    process_count, thread_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        identifier,
                        sequence,
                        sample.timestamp.isoformat(),
                        sample.rss_bytes,
                        sample.cpu_percent,
                        sample.process_count,
                        sample.thread_count,
                    )
                    for sequence, sample in enumerate(report.metrics)
                ),
            )
            connection.execute(
                """
                INSERT INTO run_contexts (run_id, repository_json, environment_json)
                VALUES (?, ?, ?)
                """,
                (
                    identifier,
                    json.dumps(repository_payload, sort_keys=True),
                    json.dumps(environment_payload, sort_keys=True),
                ),
            )
            connection.executemany(
                """
                INSERT INTO artifacts (
                    run_id, change_kind, path, size_bytes, modified_ns, sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                self._artifact_rows(identifier, report),
            )
            connection.executemany(
                """
                INSERT INTO run_states (
                    run_id, sequence, previous_state, current_state, cause, actor, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        identifier,
                        sequence,
                        transition.previous.value,
                        transition.current.value,
                        transition.cause,
                        transition.actor,
                        transition.timestamp.isoformat(),
                    )
                    for sequence, transition in enumerate(transitions)
                ),
            )
        return identifier

    def load(self, run_id: str) -> StoredRun:
        """Load a portable report by identifier or raise `KeyError`."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT report_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        payload = cast("StoredRun", json.loads(row[0]))
        payload.setdefault("transitions", [])
        payload.setdefault("repository_facts", cast("StoredRepositoryFacts", {}))
        payload.setdefault("environment_facts", cast("StoredEnvironmentFacts", {}))
        return payload

    def _connect(self) -> sqlite3.Connection:
        """Open one foreign-key-enforcing database connection."""
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _save_lines(
        connection: sqlite3.Connection,
        run_id: str,
        stream: str,
        lines: tuple[CapturedLine, ...],
    ) -> None:
        """Persist one ordered captured stream."""
        connection.executemany(
            """
            INSERT INTO log_lines (run_id, stream, sequence, timestamp, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                (run_id, stream, sequence, line.timestamp.isoformat(), line.message)
                for sequence, line in enumerate(lines)
            ),
        )

    @staticmethod
    def _artifact_rows(run_id: str, report: RunReport) -> Iterator[tuple[object, ...]]:
        """Yield normalized rows for all artifact change classes."""
        groups = (
            ("created", report.artifacts.created),
            ("modified", report.artifacts.modified),
            ("deleted", report.artifacts.deleted),
        )
        for change_kind, artifacts in groups:
            for artifact in artifacts:
                yield _artifact_row(run_id, change_kind, artifact)


def _artifact_row(run_id: str, change_kind: str, artifact: Artifact) -> tuple[object, ...]:
    """Convert one artifact to a normalized database row."""
    return (
        run_id,
        change_kind,
        artifact.path,
        artifact.size_bytes,
        artifact.modified_ns,
        artifact.sha256,
    )

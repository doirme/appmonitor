"""Local subprocess execution and observation."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, TextIO, cast

import psutil

from appmonitor.artifacts import ArtifactChanges, compare_snapshots, snapshot_files

if TYPE_CHECKING:
    from appmonitor.models import RunSpec

_SAMPLE_INTERVAL_SECONDS = 0.05
_TERMINATION_GRACE_SECONDS = 1.0


class RunOutcome(StrEnum):
    """Deterministic process outcomes."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class CapturedLine:
    """One timestamped output line."""

    timestamp: datetime
    message: str


@dataclass(frozen=True, slots=True)
class ProcessMetrics:
    """One aggregate sample for a process tree."""

    timestamp: datetime
    rss_bytes: int
    cpu_percent: float
    process_count: int
    thread_count: int


@dataclass(frozen=True, slots=True)
class RunReport:
    """Portable deterministic report for one local execution."""

    command: tuple[str, ...]
    repository: str
    outcome: RunOutcome
    exit_code: int | None
    timed_out: bool
    started_at: datetime
    finished_at: datetime
    stdout: tuple[CapturedLine, ...]
    stderr: tuple[CapturedLine, ...]
    metrics: tuple[ProcessMetrics, ...]
    artifacts: ArtifactChanges

    @property
    def duration_seconds(self) -> float:
        """Return wall-clock execution duration in seconds."""
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def peak_rss_bytes(self) -> int:
        """Return the largest observed resident process-tree size."""
        return max((sample.rss_bytes for sample in self.metrics), default=0)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize the report as stable JSON."""
        return json.dumps(asdict(self), default=_json_default, indent=indent, sort_keys=True)


class LocalExecutor:
    """Execute a command locally while collecting deterministic observations."""

    def execute(self, spec: RunSpec) -> RunReport:
        """Run a command and return its streams, metrics, artifacts, and outcome."""
        before = snapshot_files(spec.repository)
        started_at = datetime.now(UTC)
        process = subprocess.Popen(  # noqa: S603
            spec.command,
            cwd=spec.repository,
            env={**os.environ, **spec.environment},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout: list[CapturedLine] = []
        stderr: list[CapturedLine] = []
        threads = (
            _start_stream_reader(cast("TextIO | None", process.stdout), stdout),
            _start_stream_reader(cast("TextIO | None", process.stderr), stderr),
        )
        metrics: list[ProcessMetrics] = []
        timed_out = self._observe_process(process, spec.timeout_seconds, metrics)
        for thread in threads:
            thread.join(timeout=_TERMINATION_GRACE_SECONDS)
        finished_at = datetime.now(UTC)
        artifacts = compare_snapshots(before, snapshot_files(spec.repository))
        outcome = _classify_outcome(process.returncode, timed_out=timed_out)
        return RunReport(
            command=spec.command,
            repository=str(spec.repository),
            outcome=outcome,
            exit_code=process.returncode,
            timed_out=timed_out,
            started_at=started_at,
            finished_at=finished_at,
            stdout=tuple(stdout),
            stderr=tuple(stderr),
            metrics=tuple(metrics),
            artifacts=artifacts,
        )

    def _observe_process(
        self,
        process: subprocess.Popen[str],
        timeout_seconds: float | None,
        metrics: list[ProcessMetrics],
    ) -> bool:
        """Sample a process tree until completion or timeout."""
        monitored = psutil.Process(process.pid)
        started = time.monotonic()
        while process.poll() is None:
            metrics.append(_sample_process_tree(monitored))
            if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                _terminate_process_tree(monitored)
                process.wait(timeout=_TERMINATION_GRACE_SECONDS)
                return True
            time.sleep(_SAMPLE_INTERVAL_SECONDS)
        if not metrics:
            metrics.append(_empty_metrics())
        return False


def _start_stream_reader(
    stream: TextIO | None,
    destination: list[CapturedLine],
) -> threading.Thread:
    """Start one daemon reader for a child-process stream."""
    if stream is None:
        msg = "subprocess stream was not configured"
        raise RuntimeError(msg)

    def read_lines() -> None:
        with stream:
            for line in stream:
                destination.append(  # noqa: PERF401 - each line needs its read timestamp
                    CapturedLine(datetime.now(UTC), line.rstrip("\r\n")),
                )

    thread = threading.Thread(target=read_lines, daemon=True)
    thread.start()
    return thread


def _sample_process_tree(root: psutil.Process) -> ProcessMetrics:
    """Aggregate memory, CPU, process, and thread counts for a process tree."""
    try:
        processes = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return _empty_metrics()
    rss_bytes = 0
    cpu_percent = 0.0
    thread_count = 0
    observed_count = 0
    for process in processes:
        try:
            rss_bytes += process.memory_info().rss
            cpu_percent += process.cpu_percent(interval=None)
            thread_count += process.num_threads()
            observed_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return ProcessMetrics(
        timestamp=datetime.now(UTC),
        rss_bytes=rss_bytes,
        cpu_percent=cpu_percent,
        process_count=observed_count,
        thread_count=thread_count,
    )


def _empty_metrics() -> ProcessMetrics:
    """Create a zero sample when a process finishes before first observation."""
    return ProcessMetrics(datetime.now(UTC), 0, 0.0, 0, 0)


def _terminate_process_tree(root: psutil.Process) -> None:
    """Terminate descendants before their parent, then force remaining processes."""
    processes = root.children(recursive=True)
    for process in [*processes, root]:
        with suppress(psutil.NoSuchProcess):
            process.terminate()
    _, alive = psutil.wait_procs([*processes, root], timeout=_TERMINATION_GRACE_SECONDS)
    for process in alive:
        with suppress(psutil.NoSuchProcess):
            process.kill()


def _classify_outcome(exit_code: int | None, *, timed_out: bool) -> RunOutcome:
    """Map deterministic process facts to a public outcome."""
    if timed_out:
        return RunOutcome.TIMED_OUT
    return RunOutcome.SUCCEEDED if exit_code == 0 else RunOutcome.FAILED


def _json_default(value: object) -> str:
    """Convert report-specific scalar types for JSON encoding."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    msg = f"cannot serialize {type(value).__name__}"
    raise TypeError(msg)

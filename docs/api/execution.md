# Execution and reports

## `RunSpec`

```python
RunSpec(
    repository: str | Path,
    command: Sequence[str],
    timeout_seconds: float | None = None,
    base_branch: str | None = None,
    goal_file: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    *,
    sync_environment: bool = False,
    analyze_repository: bool = False,
    git_remote: str | None = None,
)
```

`RunSpec` resolves `repository` and `goal_file` to absolute paths, freezes `command` as a tuple,
and copies `environment` into a read-only mapping. It raises `ValueError` for a missing repository,
an empty executable, or a non-positive timeout.

`environment` overlays the current process environment. `base_branch` is recorded for future Git
automation but is not acted upon in the current implementation. `sync_environment` and
`analyze_repository` opt into potentially expensive preparation steps.

`git_remote=None` is the default local-only mode. A safe name such as `"origin"` opts into the
remote publication preflight before process startup. Empty, option-like, traversal, and
whitespace-containing remote names raise `ValueError`.

## `LocalExecutor`

```python
report = LocalExecutor().execute(spec)
```

The executor:

- snapshots repository artifacts before and after execution;
- starts the exact argument vector without a shell;
- captures timestamped stdout and stderr independently;
- samples aggregate process-tree RSS, CPU, process count, and thread count;
- terminates descendants and the parent when the timeout expires;
- returns a report for success, non-zero exit, and timeout.

An executable that cannot be started raises the underlying startup exception. Output is decoded
as UTF-8 with replacement for invalid bytes.

## `RunOutcome`

`RunOutcome` is a string enum:

| Member | Value | Condition |
| --- | --- | --- |
| `SUCCEEDED` | `succeeded` | Exit code is zero |
| `FAILED` | `failed` | Exit code is non-zero |
| `TIMED_OUT` | `timed_out` | The configured timeout elapsed |

## `RunReport`

| Field | Type | Meaning |
| --- | --- | --- |
| `command` | `tuple[str, ...]` | Requested argument vector |
| `repository` | `str` | Absolute working directory |
| `outcome` | `RunOutcome` | Terminal classification |
| `exit_code` | `int | None` | Child exit status when available |
| `timed_out` | `bool` | Whether timeout handling ran |
| `started_at`, `finished_at` | `datetime` | UTC execution bounds |
| `stdout`, `stderr` | `tuple[CapturedLine, ...]` | Timestamped stream lines |
| `metrics` | `tuple[ProcessMetrics, ...]` | Process-tree samples |
| `artifacts` | `ArtifactChanges` | Created, modified, and deleted files |

Properties and methods:

- `duration_seconds`: wall-clock duration.
- `peak_rss_bytes`: maximum sampled aggregate RSS, or zero without samples.
- `to_json(indent=2)`: stable sorted JSON.

`CapturedLine(timestamp, message)` and
`ProcessMetrics(timestamp, rss_bytes, cpu_percent, process_count, thread_count)` are available
from `appmonitor.execution`.

`Artifact(path, size_bytes, modified_ns, sha256)` and
`ArtifactChanges(created, modified, deleted)` are available from `appmonitor.artifacts`.
Environment secret files matching `.env` or `.env.*` are excluded. Git repositories observe
tracked and nonignored untracked files; non-Git directories use a recursive snapshot.

## Choosing the entry point

Use `LocalExecutor` when you only need in-memory observation. Use `RunClient` for lifecycle facts,
repository identity, optional analysis and goals, default SQLite persistence, and a durable
`run_id`.

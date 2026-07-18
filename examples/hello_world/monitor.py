"""Run and persist the AppMonitor hello-world target."""

import sys
from pathlib import Path

from appmonitor import LocalExecutor, RunSpec, SQLiteRunStore


def main() -> None:
    """Observe the target and print the essential result fields."""
    repository = Path(__file__).parent.resolve()
    report = LocalExecutor().execute(
        RunSpec(
            repository=repository,
            command=(sys.executable, str(repository / "hello.py")),
            timeout_seconds=5,
        ),
    )
    database = repository.parents[1] / ".appmonitor" / "hello-world.sqlite3"
    run_id = SQLiteRunStore(database).save(report)

    print(f"run_id={run_id}")
    print(f"outcome={report.outcome.value}")
    print(f"duration_seconds={report.duration_seconds:.3f}")
    print(f"peak_rss_bytes={report.peak_rss_bytes}")
    print(f"created_artifacts={[artifact.path for artifact in report.artifacts.created]}")
    print(f"database={database}")


if __name__ == "__main__":
    main()


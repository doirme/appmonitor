# Backtester WORLD LMS monitoring trial

Date: 2026-07-29

## Target

- Repository: `C:\Users\axelc\Documents\Backtester`
- Script: `research/world_lms_minvar_research.py`
- AppMonitor run ID: `17f0cd63-b5d9-4c4e-92f0-bbf9d7157d91`
- Database: `C:\Users\axelc\Documents\Backtester\.appmonitor\runs.sqlite3`

## Result

- Outcome: `succeeded`
- Exit code: `0`
- Duration: approximately 284.6 seconds
- Rebalance dates processed: 30
- Metric samples: 3,349
- Peak observed process-tree RSS: 8,587,591,680 bytes (approximately 8.0 GiB)
- Peak aggregate CPU: 788.4 percent
- Maximum observed processes: 3
- Maximum observed threads: 123
- Captured stdout lines: 7
- Captured stderr lines: 761

The stderr stream is dominated by structured `INFO` workspace logs; its line count does not
indicate 761 errors.

## Research output

The target printed this final portfolio summary:

| Portfolio | End NAV | Total return | Annualized volatility | Maximum drawdown |
| --- | ---: | ---: | ---: | ---: |
| Benchmark | 109.362336 | 9.362336% | 13.907583% | -23.098836% |
| Simple minimum variance | 96.188584 | -3.811416% | 8.854546% | -18.073549% |

The minimum-variance portfolio reduced measured volatility and drawdown but underperformed the
benchmark by approximately 13.17 percentage points over this period.

## Where to inspect

Open the SQLite database with a SQLite browser or query these tables:

- `runs`: complete report JSON, command, outcome, and timestamps;
- `log_lines`: ordered stdout and stderr events;
- `metrics`: process-tree CPU, RSS, process count, and thread count samples;
- `run_states`: deterministic lifecycle transitions;
- `run_contexts`: Git and environment identity;
- `run_analyses`: static analysis, empty because `--analyze` was not requested.

Example query:

```sql
SELECT run_id, outcome, exit_code, started_at, finished_at
FROM runs
WHERE run_id = '17f0cd63-b5d9-4c4e-92f0-bbf9d7157d91';
```

## Findings about AppMonitor

The first attempt at sizing the repository showed 151,000 files and 14.5 GB because the artifact
collector recursively considered ignored generated trees. Before the run, the collector was
changed to use `git ls-files --cached --others --exclude-standard` in Git repositories. Backtester
then exposed only 364 relevant files for snapshots.

No artifact changes were recorded because the research outputs are under the Git-ignored
`research/workspaces/` tree. This is correct for repository-change monitoring but means a future
goal contract must explicitly declare ignored output directories when they are expected artifacts.

For future exploratory Backtester trials, use a 120-second timeout unless completion is required.


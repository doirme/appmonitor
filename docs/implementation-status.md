# Implementation status

Last updated: 2026-07-29

## Delivery policy

Each phase follows the same gate:

1. add behavioral tests before implementation;
2. verify that the new tests fail for the expected missing behavior;
3. implement the smallest coherent capability;
4. run pytest with branch coverage, Ruff, mypy, `compileall`, and package build;
5. update API and implementation documentation;
6. commit and push the atomic phase only when all checks pass.

## Implemented foundation

The project currently provides:

- immutable and validated `RunSpec` inputs;
- local subprocess execution with separate timestamped stdout and stderr;
- success, failure, and timeout classification;
- process-tree memory, CPU, process, and thread sampling;
- process-tree termination on timeout;
- before/after artifact snapshots with SHA-256 change detection;
- portable JSON reports;
- transactional SQLite persistence for reports, logs, metrics, artifacts, and lifecycle states;
- a deterministic state machine with an explicit transition graph;
- a CLI and Python API;
- hello-world examples and a detailed development tutorial.

## Phase 1: deterministic orchestration

Status: complete

This phase connects the previously independent executor, state machine, and SQLite store through
`RunClient`.

### Delivered behavior

- `RunClient.execute(spec)` performs one complete deterministic local run.
- Every run receives a UUID before persistence.
- The client records repository preparation, specification analysis, environment selection,
  process start, terminal outcome, deterministic review, and report generation.
- Process outcomes map deterministically to `SUCCEEDED`, `FAILED`, or `TIMED_OUT` states.
- The report and full transition history are saved atomically.
- SQLite now includes a normalized `run_states` table.
- Existing databases are upgraded by the idempotent `CREATE TABLE IF NOT EXISTS` schema.
- Loading an older report without transitions returns an empty transition list.
- The CLI now uses `RunClient`; every CLI run is stored by default in
  `<repository>/.appmonitor/runs.sqlite3`.
- `LocalExecutor` remains public for callers that explicitly want observation without lifecycle
  orchestration or persistence.

### Validation

- 17 tests passed.
- Total branch-aware coverage: 95.74%.
- Ruff: passed.
- mypy strict: passed for source, tests, and examples.
- Targeted orchestration, CLI, and persistence tests: passed.

## Prioritized remaining phases

| Priority | Phase | Purpose | Status |
| --- | --- | --- | --- |
| 2 | Repository and uv integration | Reconstruct repository revision and environment | Next |
| 3 | Static analysis | Index code and run deterministic quality tools | Planned |
| 4 | Goal contract | Define and evaluate explicit success conditions | Planned |
| 5 | OpenRouter foundation | Structured model calls, budgets, routing, telemetry | Planned |
| 6 | Read-only diagnostic agents | Interpret runs without write or execution rights | Planned |
| 7 | Regression-test generation | Write tests under strict path and behavior policies | Planned |
| 8 | Bounded patching | Apply constrained fixes and independently verify them | Planned |
| 9 | Git automation | Worktrees, branches, commits, pushes, and pull requests | Planned |
| 10 | Docker and service foundations | Isolation first, multi-user services later | Planned |

## Phase 2 acceptance target

Repository and uv integration will be considered complete when AppMonitor can:

- inspect a local Git repository without modifying it;
- record the current commit, branch, dirty state, and `uv.lock` SHA-256;
- locate and validate the repository's `pyproject.toml` and `uv.lock`;
- run a configurable `uv sync --frozen` preparation step;
- persist the reconstructed repository/environment facts with the run;
- expose deterministic failures when Git or uv prerequisites are missing;
- cover all behavior with local tests that do not require network access.

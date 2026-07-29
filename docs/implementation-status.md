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
| 2 | Repository and uv integration | Reconstruct repository revision and environment | Complete |
| 3 | Static analysis | Index code and run deterministic quality tools | Complete |
| 4 | Goal contract | Define and evaluate explicit success conditions | Complete |
| 5 | OpenRouter foundation | Structured model calls, budgets, routing, telemetry | Complete |
| 6 | Read-only diagnostic agents | Interpret runs without write or execution rights | Next |
| 7 | Regression-test generation | Write tests under strict path and behavior policies | Planned |
| 8 | Bounded patching | Apply constrained fixes and independently verify them | Planned |
| 9 | Git automation | Worktrees, branches, commits, pushes, and pull requests | Planned |
| 10 | Docker and service foundations | Isolation first, multi-user services later | Planned |

## Phase 2: repository and uv reproducibility

Status: complete

### Delivered behavior

- inspect a local Git repository without modifying it;
- record the current commit, branch, dirty state, and `uv.lock` SHA-256;
- locate and validate the repository's `pyproject.toml` and `uv.lock`;
- continue to support ordinary non-Git directories;
- distinguish a missing Git executable from a non-Git directory;
- run an opt-in `uv sync --frozen` preparation step;
- stop before target execution when explicitly requested environment preparation fails;
- persist the reconstructed repository/environment facts with the run;
- expose repository and environment facts in `OrchestratedRun` and CLI JSON;
- store context in the one-to-one SQLite `run_contexts` table;
- test command behavior through an injected runner without network access.

### Validation

- 21 tests passed.
- Total branch-aware coverage: 95.81%.
- Ruff: passed.
- mypy strict: passed for source, tests, and examples.

## Phase 3: deterministic static analysis

Status: complete

### Delivered behavior

- index Python modules, classes, functions, imports, signatures, and docstrings with `ast`;
- parse source without importing or executing target modules;
- retain valid-file symbols when another module has syntax or decoding errors;
- ignore generated and environment directories during AST discovery;
- run Ruff, mypy, compilation, pytest collection, and coverage through a fixed command allowlist;
- classify each tool as passed, failed, or unavailable;
- persist analysis findings and associate them with the exact repository commit;
- expose analysis in `OrchestratedRun` and CLI JSON;
- store complete analysis JSON in the one-to-one `run_analyses` table;
- keep analysis opt-in through `RunSpec.analyze_repository` and CLI `--analyze`.

### Validation

- 25 tests passed.
- Total branch-aware coverage: 96.31%.
- Ruff: passed.
- mypy strict: passed for source, tests, and examples.
- `.env` and `.env.*` are excluded from Git and artifact snapshots before LLM integration.
- Git repositories use Git's tracked and nonignored-untracked file list for artifact snapshots,
  avoiding dependency caches and generated workspaces.

## Phase 4 acceptance target

Status: complete

### Delivered behavior

- load and validate a versioned `goal.yaml` without executing arbitrary expressions;
- define expected exit status, required artifact patterns, resource budgets, and expected events;
- evaluate deterministic success checks against a `RunReport`;
- report passed, failed, and unavailable checks separately;
- calculate overall, partial, and failed outcomes without an LLM;
- persist the contract hash and evaluation with the exact run;
- reject invalid or unsafe contract syntax with actionable errors.

The CLI accepts `--goal PATH`, and the Python API exports the loader, immutable models, evaluator,
and validation exception. SQLite stores normalized goal data in `run_goals` as well as in the
portable report.

### Validation

- 33 tests passed.
- Total branch-aware coverage: 94.70%.
- Ruff: passed.
- mypy strict: passed for source and tests.

## Phase 5: OpenRouter foundation

Status: complete

### Delivered behavior

- load `OPENROUTER_API_KEY` and the existing `OPEN_ROUTER_API_KEY` alias from a local env file;
- keep credentials out of representations, telemetry, prompts, and persisted responses;
- fetch and normalize the current OpenRouter model registry;
- route by context length, structured-output capability, and estimated cost;
- reject malformed and negative sentinel prices from bounded routing;
- enforce call-count and estimated USD budgets before network access;
- request strict JSON Schema output and validate it again locally;
- calculate usage from provider-reported tokens and model prices;
- persist secret-free call telemetry in SQLite;
- inject HTTP transport and telemetry for deterministic tests.

### Real API validation

- one strict structured-output call completed successfully;
- selected model: `google/gemma-4-26b-a4b-it:free`;
- result: `{"status": "ok"}`;
- 127 total tokens and USD 0 calculated cost;
- 2.047 seconds latency;
- no key, prompt, or response content persisted in telemetry.

### Validation

- 39 tests passed.
- Total branch-aware coverage: 90.44%.
- Ruff: passed.
- mypy strict: passed for source and tests.

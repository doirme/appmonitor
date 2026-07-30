# Implementation status

Last updated: 2026-07-30

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
| 6 | Read-only diagnostic agents | Interpret runs without write or execution rights | Complete |
| 7 | Regression-test generation | Write tests under strict path and behavior policies | Complete |
| 8 | Bounded patching | Apply constrained fixes and independently verify them | Complete |
| 9a | Git V1 | Worktrees, bounded local branches and commits | Complete |
| 9b | Remote Git automation | Pushes, pull requests, and approval transport | Deferred |
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

## Phase 6: read-only diagnostic agents

Status: complete

### Delivered behavior

- separate run-critic and incident-analyst roles with closed JSON schemas;
- expose only structured completion, never tools, filesystem access, or command execution;
- build bounded diagnostic context from already observed runtime, goal, repository, and analysis
  facts;
- redact common API-key, token, password, and secret patterns from command and log excerpts;
- exclude source code, artifact contents, environment variables, and docstrings from prompts;
- trigger incident analysis from deterministic failure/goal facts or high-severity critic output;
- retry malformed provider responses against the next cost-ranked compatible model;
- charge every attempt against one shared call and cost budget;
- persist assessment and optional incident JSON against the exact run ID.

### Real API validation

- deliberate exit-code-2 run: `370098a5-9ad3-4a29-9c80-b4f2866bb32f`;
- critic confidence: 1.00 with one finding;
- incident classified `runtime_error` at high priority;
- two successful structured calls with USD 0 calculated cost.

### Validation

- 43 tests passed.
- Total branch-aware coverage: 91.24%.
- Ruff: passed.
- mypy strict: passed for source and tests.

## Phase 7: bounded regression-test generation

Status: complete

### Delivered behavior

- give the test architect only structured diagnostics and an explicit bounded source context;
- request one new pytest file through a closed structured-output schema;
- require a repository-local `tests/**/test_*.py` path and reject traversal or symlinks;
- parse generated content with AST before writing;
- reject network/process imports, dynamic execution, dunder access, and direct filesystem
  mutations;
- require a real `test_*` function and assertion or `pytest.raises`;
- create authorized files exclusively, never overwrite existing tests;
- run one fixed `uv run python -m pytest` command with a 120-second timeout;
- retain only tests producing pytest exit code 1 and delete every other candidate;
- persist path, content hash, behavior, rationale, status, and exit code against the source run.

### Real API validation

- synthetic faulty calculator run: `19234866-05e3-4c40-8422-1787118b5aa1`;
- generated `tests/test_calculator.py`;
- retained after one proven pytest failure;
- two model attempts and USD 0.00008825 calculated cost.

### Validation

- 55 tests passed.
- Total branch-aware coverage: 91.05%.
- Ruff: passed.
- mypy strict: passed for source and tests.

## Phase 8: bounded transactional patching

Status: complete

### Delivered behavior

- separate structured planner, implementer, and reviewer roles;
- dynamically constrain planner and implementer path schemas to explicit source scope;
- permit only one to three existing non-test Python source files;
- bind replacements to exact original-byte SHA-256 values;
- reject traversal, symlinks, stale hashes, unchanged files, invalid syntax, oversized files, and
  patches above 200 changed lines;
- apply authorized bytes atomically with permission preservation;
- roll back exact originals after exceptions, deterministic failures, or reviewer rejection;
- run regression, full pytest, Ruff, mypy, and compilation under fixed commands and 120-second
  limits;
- persist plan, diff, patch hash, validation, review, status, and reason against the source run;
- perform no Git, branch, worktree, commit, or push operation.

### Real API validation

- calculator repair run: `050c862b-b7f3-4660-b93f-c1d6d7e6bc89`;
- changed subtraction to addition in one existing source file;
- all five deterministic checks passed;
- independent reviewer verdict: `approve`;
- three model calls and USD 0.001578 calculated cost;
- patch SHA-256:
  `01fec79a34ec951d5da8a9cf8b471b555dddce2915f01b7440f3e9344b83e6cd`.

### Validation

- 63 tests passed.
- Total branch-aware coverage: 91.44%.
- Ruff lint and format: passed.
- mypy strict: passed for source and tests.
- `compileall`, uv lock check, and package build: passed.

### Five-cycle optimization pass

- applied canonical Ruff formatting across source, tests, and examples;
- reduced formatting-only line noise in existing modules;
- retained injected, single-purpose boundaries for model calls, policy, mutation, validation, and
  persistence;
- confirmed systematic public class/function docstrings through Ruff's `ALL` rule set;
- introduced no Git automation, Docker, or service code beyond the requested phase-8 boundary.

## Documentation and observability pass

Status: complete

- added a documentation home and ordered learning path;
- split the public API reference by execution, orchestration, goals/analysis, OpenRouter,
  maintenance, persistence, and CLI;
- documented signatures, return values, errors, effects, persistence tables, and current limits;
- added an end-to-end maintenance tutorial through bounded patching;
- added standard-library SQLite recipes for runtime performance, model reliability, latency,
  token usage, and cost analysis;
- documented that registry fetching is explicit and currently uncached;
- documented that model ranking is capability-and-cost based and does not yet learn from
  historical telemetry;
- retained the phase-8 boundary: no Git automation, Docker, dashboard, or service implementation.

### Validation

- all local Markdown links resolve;
- 63 tests passed;
- total branch-aware coverage: 91.30%;
- Ruff lint and format: passed;
- mypy strict: passed for source, tests, and examples;
- `compileall`, uv lock check, and package build: passed.

## V1 phase 1: in-process instrumentation

Status: complete

- added the optional `@monitored` decorator without changing return or exception behavior;
- capture bounded arguments, return identity, sanitized exceptions, timing, and RSS variation;
- compare repository artifacts with declared required output patterns;
- evaluate runtime and memory-delta budgets as observations;
- redact secret-bearing parameter names and common OpenRouter key values;
- compare a call with an explicit prior `CallReference`;
- persist observations through in-memory and SQLite recorders;
- documented the API, limitations, and first function-level use case.

### Validation

- 68 tests passed;
- total branch-aware coverage: 91.48%;
- Ruff lint and format, mypy strict, and `compileall`: passed;
- package build intentionally deferred to the final V1 gate.

## V1 phase 2: reference-filtered OpenRouter routing

Status: complete

- resolve a configurable reference model from the live OpenRouter registry;
- default `OPENROUTER_REFERENCE_MODEL` to `openai/gpt-oss-120b` outside business logic;
- require candidate context and knowledge cutoff to meet or exceed the reference;
- reject expired models using validated ISO API dates;
- require a configurable rolling 30-minute endpoint availability percentage;
- require the configured/reference Artificial Analysis Coding Index when at least ten models and
  the reference expose valid scores;
- retain existing structured-output, context, price, budget, and cost-order behavior;
- use only official `/models` and model-endpoints JSON APIs, without HTML scraping;
- reject absent or incomplete reference configuration explicitly.

### Real API validation

- reference resolved with 131,072 context tokens, 2024-06-30 cutoff, and coding index 30.4;
- 14 current models passed all reference filters;
- reference endpoint availability observed at 100% over the 30-minute API window;
- a one-attempt structured call exposed an invalid provider response;
- the existing bounded fallback succeeded with `deepseek/deepseek-v3.1-terminus`;
- validated result `{"status": "ok"}`, 191 tokens, USD 0.00005887, 1.906 seconds.

### Validation

- 83 tests passed;
- total branch-aware coverage: 90.67%;
- Ruff lint and format, mypy strict, and `compileall`: passed;
- package build intentionally deferred to the final V1 gate.

## V1 phase 3: isolated Git completion

Status: complete

- require a valid clean source repository and resolve its exact base commit;
- create a detached worktree under `.appmonitor/worktrees/<run-id>`;
- run regression generation and bounded patching against the isolated path;
- create the dedicated `appmonitor/<run-id>` branch only after accepted maintenance;
- reject changed files outside explicit source scope and the generated regression;
- stage exact observed paths and create one atomic local commit;
- remove accepted and rejected worktrees while preserving accepted local branches;
- persist the final decision in `run_git_maintenance` against the source run;
- perform no push, pull request, remote branch update, or remote approval action.

### Validation

- 93 tests passed, including real local Git repositories and worktrees;
- total branch-aware coverage: 90.31%;
- Ruff lint and format, mypy strict, and `compileall`: passed;
- package build intentionally deferred to the final V1 gate.

## V1 final acceptance

Status: complete

V1 now includes deterministic process monitoring, optional in-process instrumentation,
reference-filtered OpenRouter routing, read-only diagnostics, bounded regression generation,
transactional patching, isolated Git worktrees, and an accepted local branch/commit.

### Final gate

- uv lock check: passed;
- 93 tests passed;
- total branch-aware coverage: 90.31%;
- Ruff lint and format: passed;
- mypy strict: passed for source, tests, and examples;
- `compileall`: passed;
- the single final Hatchling build produced the source archive and wheel;
- the wheel contains both new V1 modules: `instrumentation.py` and `git_workflow.py`.

Remote push/PR automation is deferred and opt-in. Docker isolation is the next planned product
phase; the multi-user service remains later.

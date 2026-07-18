# AppMonitor: initial product and development plan

## Objective

AppMonitor is an intelligent execution platform for Python programs. It observes a run,
reconstructs its context, identifies incidents and improvement opportunities, and can later
coordinate specialized agents that propose tests and bounded code changes.

The first acceptance principle is auditability: for any run, the system must be able to
reconstruct the code revision, environment, command, events, artifacts, decisions, model
costs, patches, and validations that produced the final result.

The deterministic orchestrator is the source of decisions. Models return validated,
structured recommendations; they never mutate state or execute commands directly.

## Public interfaces

### Command line

```bash
appmonitor run \
  --repo /path/to/repository \
  --base-branch main \
  --goal goal.yaml \
  -- uv run python scripts/train.py
```

The CLI is the primary interface because it can observe import failures, child processes,
containerized targets, and abrupt termination without modifying target code.

### Decorator

```python
from appmonitor import OutputArtifact, ResourceBudget, monitored

@monitored(
    goal="Build a monthly forecast",
    outputs=[OutputArtifact(pattern="outputs/forecast_*.parquet", required=True)],
    budget=ResourceBudget(max_memory_mb=4_000, max_runtime_seconds=900),
)
def build_forecast() -> None:
    ...
```

The decorator enriches observations with signatures, annotations, arguments, return values,
exceptions, timing, memory variation, and file changes. It remains optional because it cannot
observe failures before invocation or all native and subprocess activity.

### Python API

```python
from appmonitor import RunClient, RunSpec

run = RunClient().execute(
    RunSpec(
        repository="./project",
        command=("uv", "run", "python", "main.py"),
        base_branch="main",
        goal_file="goal.yaml",
    )
)
```

## Architecture

```text
CLI / Python API / decorators
             |
             v
   deterministic orchestrator
             |
     Git / execution / static analysis
             |
             v
        normalized event bus
             |
      database + raw artifacts
             |
             v
       specialized LLM agents
             |
             v
    deterministic validation
             |
             v
      commit / push / pull request
             |
             v
      Markdown documentation
```

SQLite is the initial source of truth. Markdown files are generated projections. PostgreSQL
is deferred until concurrent workers, multiple users, access policies, or vector search make
it necessary.

## Explicit goal contract

Success should not be inferred from docstrings alone. A `goal.yaml` contract describes the
expected process result, artifacts, assertions, budgets, and significant log events.

```yaml
goal:
  description: Produce daily risk-factor estimates.
success:
  process:
    exit_code: 0
  artifacts:
    - path: outputs/factor_returns.parquet
      required: true
      min_rows: 250
      required_columns: [date, factor, return]
  assertions:
    - expression: artifact.factor_returns.null_ratio < 0.01
resources:
  max_runtime_seconds: 1800
  max_peak_memory_mb: 8000
monitoring:
  expected_log_events: [data_loaded, optimization_completed, output_saved]
```

An inferred goal includes confidence, evidence, proposed checks, and ambiguities. Low
confidence can request human clarification but cannot authorize a repository modification.

## State machine

```text
CREATED -> REPOSITORY_PREPARED -> ANALYZED -> ENVIRONMENT_READY -> RUNNING
RUNNING -> SUCCEEDED | FAILED | TIMED_OUT | RESOURCE_LIMIT_EXCEEDED
terminal run -> REVIEWED -> PATCH_PLANNED -> PATCH_APPLIED -> VERIFYING
VERIFYING -> VERIFIED | REJECTED
VERIFIED -> COMMITTED -> PUSHED -> REPORTED
```

Each transition records its cause, actor, input artifacts, LLM call identifier and cost when
applicable, Git revision, and timestamp. The orchestrator rejects transitions outside the
declared graph.

## Deterministic runtime collection

The local executor launches an explicit child command with a fixed working directory and a
filtered environment. It records stdout and stderr separately, timestamps lines, captures the
exit code, enforces a timeout, and terminates the process tree. Periodic samples cover CPU,
memory, threads, I/O, open descriptors when available, and child-process counts.

Filesystem snapshots before and after execution identify created, changed, and deleted
artifacts with sizes, timestamps, and hashes. Large, binary, secret-bearing, or customer data
is never sent automatically to a model.

Optional injected Python instrumentation may install exception, warning, structured logging,
and `faulthandler` hooks. Audit hooks provide observability, not a security sandbox.

Raw logs are normalized and aggregated before any model call. Deterministic triggers include
exceptions, stalls, sustained memory growth, missing artifacts, broken invariants, missing
milestones, process termination, and divergence from a reference run.

## Specialized agents

| Agent | Responsibility | Write permission |
| --- | --- | --- |
| Repository analyst | Map files, symbols, commands, and tests | None |
| Goal analyst | Propose goal and success checks | None |
| Run critic | Compare events, metrics, artifacts, and goal | None |
| Incident analyst | Classify and localize failures | None |
| Test architect | Create a reproducing regression test | Tests only |
| Patch planner | Produce a bounded ordered plan | None |
| Implementer | Apply an approved plan in a worktree | Approved files |
| Reviewer | Search for regressions and security issues | None |
| Report editor | Project records to Markdown | Reports only |

Critical changes use an independent reviewer model family to reduce correlated reasoning.

## OpenRouter model routing

All agents share an OpenRouter client but select models by declared capabilities rather than
hard-coded brand names. The model registry records context length, structured-output and tool
support, price, latency observations, and task-specific quality. This permits experiments with
GLM, Qwen, KAT-Coder, GPT, Claude, or future models without changing agent code.

Routing starts with the least costly model meeting the capability policy and escalates on low
confidence, schema failure, failed validation, critical severity, or exhausted retry budget.
Every run defines `max_cost_usd`, `max_calls`, and `max_critical_reviews`.

### Cost and efficiency dashboard

Per model, role, repository, and time range, expose:

- input/output tokens, total cost, latency, and provider errors;
- valid structured responses on first attempt;
- test, Ruff, mypy, and compilation passes needed after a generated change;
- implementation-to-verification round trips;
- accepted, rejected, reverted, and human-edited patches;
- reviewer score, regression count, diff size, and time to verified result;
- cost per verified fix and verified fixes per million tokens.

An implementer efficiency score must use deterministic outcomes first. A proposed baseline is
the weighted combination of first-pass validation, inverse validation round trips, reviewer
score, regression-free acceptance, and normalized cost. Raw components remain visible so a
single composite score cannot hide poor quality or excessive expense.

## Repository and validation workflow

Automated changes happen in `.appmonitor/worktrees/<run-id>` on a dedicated branch. The system
records the clean base revision, runs baseline checks, writes a failing regression test, applies
the bounded patch, reruns all checks, obtains independent review, and only then commits. Push
and pull-request creation are later capabilities and require branch policies and human approval
for sensitive changes.

The standard quality gate is:

```bash
uv sync --frozen
uv run pytest --cov --cov-branch
uv run ruff check .
uv run mypy src tests
uv run python -m compileall -q src tests
uv build
```

## Docker and service architecture

The trusted orchestrator controls Docker from outside the target container. Target containers
run as a non-root user, read-only where possible, with dropped capabilities, no new privileges,
bounded CPU/memory/PIDs/time, isolated temporary storage, no secrets, and no network by default.
The Docker socket is never mounted in an untrusted execution container.

The eventual service split is: API/control plane, execution workers, LLM workers, PostgreSQL,
and object storage. FastAPI, authentication, quotas, multi-user permissions, and a web UI are
deferred until the single-user execution pipeline is stable.

## Delivery sequence

1. Deterministic core: run specification, states, local execution, logs, metrics, artifacts,
   JSON report, and SQLite persistence.
2. Repositories and uv: clone/local repositories, revisions, worktrees, frozen sync, lock hash.
3. Static analysis: AST index, Ruff, mypy, compilation, pytest collection, branch coverage.
4. Goal contract: artifact checks, assertions, budgets, partial success, reference comparison.
5. Read-only run critic producing a validated `RunAssessment`.
6. Incident classification, localization, minimal reproduction, confidence, and priority.
7. Test generation restricted to `tests/`, with deterministic rejection rules.
8. Bounded patches, complete validation, and independent review.
9. Atomic commit, dedicated-branch push, pull request, and severity-based approval.
10. Isolated Docker execution, stats, artifact export, cache, and guaranteed cleanup.
11. Multi-user service, workers, PostgreSQL, authentication, quotas, and audit log.

## Version-one boundary

Version one supports a local or Git Python repository, a reproducible uv environment, an
explicit command, local or Docker execution, logs/process metrics/artifacts, static analysis,
an execution report, LLM diagnosis, regression-test generation, a worktree patch, validation,
and a local commit. Automatic pushes, pull requests, multi-user operation, and vector memory
remain opt-in later milestones.

## Origin

This specification consolidates the initial design discussion shared at
<https://chatgpt.com/share/6a5b94c0-385c-83eb-94e7-afeb1b5729cf> on 2026-07-18.


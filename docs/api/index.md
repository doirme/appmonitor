# API reference

The supported API is exported from `appmonitor`. It is grouped below by responsibility.

## Execution

| Name | Purpose |
| --- | --- |
| `RunSpec` | Validated immutable command specification |
| `LocalExecutor` | Low-level subprocess observation |
| `RunOutcome` | `succeeded`, `failed`, or `timed_out` |
| `RunReport` | Streams, metrics, artifacts, timing, and outcome |

See [Execution and reports](execution.md).

## In-process instrumentation

| Name | Purpose |
| --- | --- |
| `monitored` | Optional function decorator |
| `OutputArtifact` | Expected changed-path declaration |
| `ResourceBudget` | Runtime and RSS-delta checks |
| `CallObservation` | Bounded function-call evidence |
| `CallReference` | Explicit comparison baseline |
| `InMemoryCallRecorder` | In-memory observation destination |
| `SQLiteInstrumentationStore` | Durable call observations |

See [In-process instrumentation](instrumentation.md).

## Orchestration and deterministic analysis

| Name | Purpose |
| --- | --- |
| `RunClient` | Complete lifecycle, execution, and persistence |
| `OrchestratedRun` | Enriched result returned by `RunClient` |
| `RepositoryFacts` | Git and project identity |
| `EnvironmentFacts` | Interpreter and optional `uv sync` result |
| `StaticAnalyzer` | AST indexing and optional fixed quality gate |
| `StaticAnalysisReport` | Symbols, imports, syntax findings, and tool checks |
| `GoalContract` | Validated deterministic success criteria |
| `GoalEvaluator` | Compare a contract with a report |
| `GoalEvaluation` | Aggregate and individual check results |
| `GoalContractError` | Invalid goal file |
| `load_goal_contract` | Safe YAML contract loader |

See [Orchestration](orchestration.md) and [Goals and static analysis](goals-and-analysis.md).

## OpenRouter

| Name | Purpose |
| --- | --- |
| `OpenRouterConfig` | Secret-bearing network configuration |
| `fetch_model_registry` | Explicit model catalog request |
| `ModelRegistry` | Immutable capability and price inventory |
| `ModelRequirements` | Context, output, and capability constraints |
| `ModelRoutingConstraints` | Hard independent-review restrictions |
| `ModelTaskStats` | Task-specific adaptive routing evidence |
| `OpenRouterClient` | Budgeted, structured completion client |
| `ChatMessage` | Typed chat input |
| `StructuredCompletion` | Validated data, usage, model, and latency |
| `LLMBudget` | Shared call-count and cost guard |
| `SQLiteLLMTelemetry` | Secret-free call records |
| `BudgetExceededError` | Call or estimated-cost limit reached |
| `StructuredOutputError` | Invalid JSON or schema mismatch |

See [OpenRouter](openrouter.md).

## Maintenance stages

| Stage | Public names |
| --- | --- |
| Diagnostics | `DiagnosticPipeline`, `DiagnosticResult`, `RunAssessment`, `IncidentAnalysis`, `DiagnosticFinding`, `SQLiteDiagnosticStore`, `build_diagnostic_context` |
| Regression | `RegressionTestGenerator`, `GeneratedTestPolicy`, `RegressionTestWorkflow`, `RegressionTestResult`, `TestProposal`, `SQLiteRegressionStore`, `TestPolicyError`, `collect_source_context` |
| Patching | `PatchPlannerAgent`, `PatchImplementerAgent`, `PatchReviewerAgent`, `PatchPipeline`, `PatchPipelineResult`, `PatchPlan`, `PatchPolicy`, `AtomicPatchApplier`, `SQLitePatchStore`, `PatchPolicyError` |

See [Diagnostic and maintenance pipelines](maintenance.md).

## Git publication and local recovery

| Name | Purpose |
| --- | --- |
| `GitMaintenanceWorkflow` | Regression, patching, commit, optional push and restart |
| `GitRemotePublisher` | Startup dry-run preflight and dedicated-branch push |
| `GitWorktreeManager` | Managed detached worktrees and scoped commits |
| `GitMaintenanceResult` | Complete rejected, committed, or pushed decision |
| `GitCommitResult` | Branch, commit, base, and exact changed paths |
| `SQLiteGitStore` | Durable Git maintenance audit |
| `GitAutomationError` | Unsafe scope, state, path, or Git failure |
| `RecoveryDecisionAgent` | Structured restart or stop recommendation |
| `RecoveryDecision` | Auditable recovery action and confidence |
| `RecoveryLimits` | Restart count and elapsed-time bounds |
| `RecoveryLimitError` | Exhausted recovery boundary |

See [Isolated Git maintenance](git-workflow.md).

## Persistence

`SQLiteRunStore` persists normalized run evidence. The diagnostic, regression, patch, and LLM
stores add their own projections and can share the same database path.

See [SQLite persistence](persistence.md) and the [CLI reference](cli.md).

## Stability rule

The names above are exported by:

```python
from appmonitor import RunClient, RunSpec
```

Supporting types such as `CapturedLine`, `ProcessMetrics`, `RunState`, and `StateTransition` are
documented because they occur in public return values, but currently require module-level imports.

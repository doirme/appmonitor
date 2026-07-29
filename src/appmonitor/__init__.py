"""Public AppMonitor API."""

from appmonitor.analysis import StaticAnalysisReport, StaticAnalyzer
from appmonitor.execution import LocalExecutor, RunOutcome, RunReport
from appmonitor.goal import (
    GoalContract,
    GoalContractError,
    GoalEvaluation,
    GoalEvaluator,
    load_goal_contract,
)
from appmonitor.models import RunSpec
from appmonitor.openrouter import (
    BudgetExceededError,
    ChatMessage,
    LLMBudget,
    ModelRegistry,
    ModelRequirements,
    OpenRouterClient,
    OpenRouterConfig,
    SQLiteLLMTelemetry,
    StructuredCompletion,
    StructuredOutputError,
    fetch_model_registry,
)
from appmonitor.orchestrator import OrchestratedRun, RunClient
from appmonitor.persistence import SQLiteRunStore
from appmonitor.repository import EnvironmentFacts, RepositoryFacts

__all__ = [
    "BudgetExceededError",
    "ChatMessage",
    "EnvironmentFacts",
    "GoalContract",
    "GoalContractError",
    "GoalEvaluation",
    "GoalEvaluator",
    "LLMBudget",
    "LocalExecutor",
    "ModelRegistry",
    "ModelRequirements",
    "OpenRouterClient",
    "OpenRouterConfig",
    "OrchestratedRun",
    "RepositoryFacts",
    "RunClient",
    "RunOutcome",
    "RunReport",
    "RunSpec",
    "SQLiteLLMTelemetry",
    "SQLiteRunStore",
    "StaticAnalysisReport",
    "StaticAnalyzer",
    "StructuredCompletion",
    "StructuredOutputError",
    "fetch_model_registry",
    "load_goal_contract",
]

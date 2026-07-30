"""Bounded structured OpenRouter calls with capability routing and telemetry."""

from __future__ import annotations

import json
import math
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

import jsonschema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_API_BASE_URL = "https://openrouter.ai/api/v1"
_KEY_NAMES = ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")
_DEFAULT_REFERENCE_MODEL = "openai/gpt-oss-120b"
_DEFAULT_MIN_CODING_INDEX = 0.0
_DEFAULT_ADAPTIVE_MIN_SAMPLES = 3
_DEFAULT_REVIEWER_MODELS = (
    "z-ai/glm-5.2",
    "google/gemini-3.6-flash",
    "openai/gpt-5.6-terra",
    "x-ai/grok-4.5",
    "anthropic/claude-opus-5",
    "deepseek/deepseek-v4-pro",
    "moonshotai/kimi-k2.7-code",
    "qwen/qwen3-coder-next",
)
_MIN_CODING_COVERAGE = 10
_PERCENT_MAX = 100.0
_ISO_MONTH_LENGTH = 7
_ISO_DATE_LENGTH = 10


class OpenRouterError(RuntimeError):
    """Base error for OpenRouter infrastructure failures."""


class ConfigurationError(OpenRouterError):
    """Raised when required OpenRouter configuration is absent or invalid."""


class NoCompatibleModelError(OpenRouterError):
    """Raised when no registered model meets declared requirements."""


class BudgetExceededError(OpenRouterError):
    """Raised before a call that would exceed its deterministic budget."""


class StructuredOutputError(OpenRouterError):
    """Raised when a model response is not valid against the requested schema."""


@dataclass(frozen=True, slots=True)
class OpenRouterConfig:
    """Authentication and network settings without secret-bearing representation."""

    api_key: str = field(repr=False)
    base_url: str = _API_BASE_URL
    timeout_seconds: float = 30.0
    app_name: str = "AppMonitor"
    site_url: str | None = None
    reference_model_id: str = _DEFAULT_REFERENCE_MODEL
    min_coding_index: float = _DEFAULT_MIN_CODING_INDEX
    reviewer_model_ids: tuple[str, ...] = _DEFAULT_REVIEWER_MODELS
    critical_review_different_provider: bool = True
    adaptive_min_samples: int = _DEFAULT_ADAPTIVE_MIN_SAMPLES

    def __post_init__(self) -> None:
        """Reject unusable configuration."""
        if not self.api_key.strip():
            message = "OpenRouter API key must not be empty"
            raise ConfigurationError(message)
        if self.timeout_seconds <= 0:
            message = "OpenRouter timeout must be greater than zero"
            raise ConfigurationError(message)
        if not self.reference_model_id.strip():
            message = "OpenRouter reference model must not be empty"
            raise ConfigurationError(message)
        if not 0 <= self.min_coding_index <= _PERCENT_MAX:
            message = "OpenRouter minimum coding index must be between 0 and 100"
            raise ConfigurationError(message)
        if not self.reviewer_model_ids or any(
            not model_id.strip() for model_id in self.reviewer_model_ids
        ):
            message = "OpenRouter reviewer model allowlist must not be empty"
            raise ConfigurationError(message)
        if len(set(self.reviewer_model_ids)) != len(self.reviewer_model_ids):
            message = "OpenRouter reviewer model allowlist contains duplicates"
            raise ConfigurationError(message)
        if self.adaptive_min_samples <= 0:
            message = "OpenRouter adaptive minimum samples must be greater than zero"
            raise ConfigurationError(message)

    @classmethod
    def from_env_file(cls, path: str | Path) -> OpenRouterConfig:
        """Load either supported API-key variable from a local dotenv-style file."""
        values = _read_env_file(Path(path))
        api_key = next((values[name] for name in _KEY_NAMES if values.get(name)), None)
        if api_key is None:
            names = " or ".join(_KEY_NAMES)
            message = f"{path} must define {names}"
            raise ConfigurationError(message)
        return cls(
            api_key=api_key,
            reference_model_id=values.get(
                "OPENROUTER_REFERENCE_MODEL",
                _DEFAULT_REFERENCE_MODEL,
            ),
            min_coding_index=_environment_float(
                values,
                "OPENROUTER_MIN_CODING_INDEX",
                _DEFAULT_MIN_CODING_INDEX,
            ),
            reviewer_model_ids=_environment_list(
                values,
                "OPENROUTER_REVIEWER_MODELS",
                _DEFAULT_REVIEWER_MODELS,
            ),
            critical_review_different_provider=_environment_bool(
                values,
                "OPENROUTER_CRITICAL_REVIEW_DIFFERENT_PROVIDER",
                default=True,
            ),
            adaptive_min_samples=_environment_int(
                values,
                "OPENROUTER_ADAPTIVE_MIN_SAMPLES",
                _DEFAULT_ADAPTIVE_MIN_SAMPLES,
            ),
        )

    @property
    def selection_policy(self) -> ModelSelectionPolicy:
        """Return the reference policy represented by this configuration."""
        return ModelSelectionPolicy(
            reference_model_id=self.reference_model_id,
            min_coding_index=self.min_coding_index,
        )


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One text message sent to a chat-completion model."""

    role: Literal["system", "user", "assistant"]
    content: str

    def to_dict(self) -> dict[str, str]:
        """Return the OpenAI-compatible wire shape."""
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class ModelRequirements:
    """Capabilities and size needed for one task."""

    min_context_tokens: int = 8_000
    structured_output: bool = True
    estimated_input_tokens: int = 1
    max_output_tokens: int = 1_000


@dataclass(frozen=True, slots=True)
class ModelRoutingConstraints:
    """Per-call restrictions applied before adaptive model ranking."""

    reviewer_author_model_id: str | None = None
    critical_review: bool = False

    @classmethod
    def reviewer(
        cls,
        *,
        author_model_id: str,
        critical: bool = False,
    ) -> ModelRoutingConstraints:
        """Require a reviewer independent from the patch author."""
        if not author_model_id.strip():
            message = "reviewer author model ID must not be empty"
            raise ValueError(message)
        return cls(
            reviewer_author_model_id=author_model_id,
            critical_review=critical,
        )


@dataclass(frozen=True, slots=True)
class ModelSelectionPolicy:
    """Reference-relative quality eligibility thresholds."""

    reference_model_id: str = _DEFAULT_REFERENCE_MODEL
    min_coding_index: float = _DEFAULT_MIN_CODING_INDEX

    def __post_init__(self) -> None:
        """Reject ambiguous policy values."""
        if not self.reference_model_id.strip():
            message = "reference model ID must not be empty"
            raise ConfigurationError(message)
        if not 0 <= self.min_coding_index <= _PERCENT_MAX:
            message = "minimum coding index must be between 0 and 100"
            raise ConfigurationError(message)


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Routing facts for one OpenRouter model."""

    model_id: str
    context_length: int
    prompt_price_per_token: float
    completion_price_per_token: float
    supported_parameters: frozenset[str]
    knowledge_cutoff: date | None = None
    expiration_date: date | None = None
    coding_index: float | None = None

    def supports(self, requirements: ModelRequirements) -> bool:
        """Return whether the model satisfies declared capabilities."""
        has_structured = (
            "structured_outputs" in self.supported_parameters
            or "response_format" in self.supported_parameters
        )
        return self.context_length >= requirements.min_context_tokens and (
            not requirements.structured_output or has_structured
        )

    def estimated_cost(self, requirements: ModelRequirements) -> float:
        """Estimate USD cost from declared token bounds."""
        return (
            requirements.estimated_input_tokens * self.prompt_price_per_token
            + requirements.max_output_tokens * self.completion_price_per_token
        )

    def actual_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Calculate USD cost from provider-reported token usage."""
        return (
            prompt_tokens * self.prompt_price_per_token
            + completion_tokens * self.completion_price_per_token
        )


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    """Immutable model inventory used by deterministic routing."""

    models: tuple[ModelInfo, ...]
    reviewer_models: tuple[ModelInfo, ...] = ()

    @classmethod
    def from_api_response(cls, response: Mapping[str, object]) -> ModelRegistry:
        """Parse usable text-model facts from the OpenRouter models response."""
        raw_models = response.get("data")
        if not isinstance(raw_models, list):
            message = "OpenRouter models response must contain a data list"
            raise OpenRouterError(message)
        models = tuple(model for item in raw_models if (model := _parse_model(item)) is not None)
        if not models:
            message = "OpenRouter returned no routable models"
            raise NoCompatibleModelError(message)
        return cls(models)

    def select(self, requirements: ModelRequirements) -> ModelInfo:
        """Choose the least expensive compatible model with stable tie-breaking."""
        return self.rank(requirements)[0]

    def apply_policy(
        self,
        policy: ModelSelectionPolicy,
        *,
        today: date | None = None,
    ) -> ModelRegistry:
        """Filter against a complete reference before cost-based task ranking."""
        current_date = today or datetime.now(UTC).date()
        reference, coding_enabled = _reference_policy(self.models, policy, current_date)
        resolved = _ResolvedPolicy(
            reference=reference,
            today=current_date,
            coding_enabled=coding_enabled,
            coding_floor=max(policy.min_coding_index, reference.coding_index or 0.0),
        )
        eligible = tuple(model for model in self.models if _meets_reference_policy(model, resolved))
        if not eligible:
            message = "no model satisfies the configured reference policy"
            raise NoCompatibleModelError(message)
        return ModelRegistry(eligible, reviewer_models=self.models)

    def rank(self, requirements: ModelRequirements) -> tuple[ModelInfo, ...]:
        """Return all compatible models ordered by estimated cost and identifier."""
        return _rank_inventory(self.models, requirements)

    def rank_reviewers(self, requirements: ModelRequirements) -> tuple[ModelInfo, ...]:
        """Rank the explicit reviewer inventory without the generation reference gate."""
        inventory = self.reviewer_models or self.models
        today = datetime.now(UTC).date()
        active = tuple(
            model
            for model in inventory
            if model.expiration_date is None or model.expiration_date > today
        )
        return _rank_inventory(active, requirements)


def _rank_inventory(
    inventory: tuple[ModelInfo, ...],
    requirements: ModelRequirements,
) -> tuple[ModelInfo, ...]:
    """Return one capability-compatible inventory in deterministic cost order."""
    compatible = tuple(model for model in inventory if model.supports(requirements))
    if not compatible:
        message = "no model satisfies context and structured-output requirements"
        raise NoCompatibleModelError(message)
    return tuple(
        sorted(
            compatible,
            key=lambda model: (model.estimated_cost(requirements), model.model_id),
        )
    )


@dataclass(slots=True)
class LLMBudget:
    """Stateful call and cost limits shared by one orchestration scope."""

    max_calls: int
    max_cost_usd: float
    calls: int = field(default=0, init=False)
    spent_usd: float = field(default=0.0, init=False)
    _reserved_usd: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        """Reject non-positive limits."""
        if self.max_calls <= 0 or self.max_cost_usd <= 0:
            message = "LLM budget limits must be greater than zero"
            raise ValueError(message)

    def begin_call(self, estimated_cost_usd: float) -> None:
        """Reserve one call before network activity."""
        if self.calls >= self.max_calls:
            message = f"LLM call limit reached ({self.max_calls})"
            raise BudgetExceededError(message)
        projected = self.spent_usd + self._reserved_usd + estimated_cost_usd
        if projected > self.max_cost_usd:
            message = f"estimated LLM cost ${projected:.6f} exceeds budget ${self.max_cost_usd:.6f}"
            raise BudgetExceededError(message)
        self.calls += 1
        self._reserved_usd += estimated_cost_usd

    def finish_call(self, estimated_cost_usd: float, actual_cost_usd: float) -> None:
        """Replace a reservation with provider-reported actual usage."""
        self._reserved_usd = max(0.0, self._reserved_usd - estimated_cost_usd)
        self.spent_usd += actual_cost_usd


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token and calculated-cost facts for one response."""

    prompt_tokens: int
    completion_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        """Return input and output tokens combined."""
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class StructuredCompletion:
    """Validated JSON result and its audit metadata."""

    call_id: str
    model: str
    data: dict[str, object]
    usage: LLMUsage
    latency_seconds: float


LLMCallStatus = Literal["succeeded", "invalid_response", "provider_error"]


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """Secret-free durable telemetry for one model call."""

    call_id: str
    task: str
    model: str
    status: LLMCallStatus
    started_at: datetime
    latency_seconds: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    prompt_sha256: str
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class ModelTaskStats:
    """Aggregated operational evidence for one model and task."""

    model: str
    samples: int
    successes: int
    invalid_responses: int
    provider_errors: int
    average_latency_seconds: float
    average_cost_usd: float

    @property
    def quality_rate(self) -> float:
        """Return a smoothed structured-output success rate."""
        evaluated = self.successes + self.invalid_responses
        return (self.successes + 1) / (evaluated + 2)

    @property
    def reliability_rate(self) -> float:
        """Return a smoothed provider-level completion rate."""
        completed = self.samples - self.provider_errors
        return (completed + 1) / (self.samples + 2)


class LLMTelemetry(Protocol):
    """Destination for secret-free LLM call measurements."""

    def record(self, call: LLMCallRecord) -> None:
        """Persist one completed call."""

    def summarize(self, task: str) -> tuple[ModelTaskStats, ...]:
        """Aggregate model evidence for one exact task."""


class NullLLMTelemetry:
    """Discard telemetry for callers that explicitly provide no store."""

    def record(self, call: LLMCallRecord) -> None:
        """Discard one record."""
        del call

    def summarize(self, task: str) -> tuple[ModelTaskStats, ...]:
        """Return no historical evidence."""
        del task
        return ()


_TELEMETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    latency_seconds REAL NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    error_type TEXT
);
"""


class SQLiteLLMTelemetry:
    """Persist LLM telemetry without prompts, responses, or credentials."""

    def __init__(self, database: str | Path) -> None:
        """Initialize the telemetry table."""
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(_TELEMETRY_SCHEMA)

    def record(self, call: LLMCallRecord) -> None:
        """Insert one immutable call record."""
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                """
                INSERT INTO llm_calls (
                    call_id, task, model, status, started_at, latency_seconds,
                    prompt_tokens, completion_tokens, cost_usd, prompt_sha256, error_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call.call_id,
                    call.task,
                    call.model,
                    call.status,
                    call.started_at.isoformat(),
                    call.latency_seconds,
                    call.prompt_tokens,
                    call.completion_tokens,
                    call.cost_usd,
                    call.prompt_sha256,
                    call.error_type,
                ),
            )

    def list_calls(self) -> tuple[LLMCallRecord, ...]:
        """Return calls in insertion order for reporting and tests."""
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                """
                SELECT call_id, task, model, status, started_at, latency_seconds,
                       prompt_tokens, completion_tokens, cost_usd, prompt_sha256, error_type
                FROM llm_calls ORDER BY rowid
                """,
            ).fetchall()
        return tuple(
            LLMCallRecord(
                call_id=row[0],
                task=row[1],
                model=row[2],
                status=cast("LLMCallStatus", row[3]),
                started_at=datetime.fromisoformat(row[4]),
                latency_seconds=row[5],
                prompt_tokens=row[6],
                completion_tokens=row[7],
                cost_usd=row[8],
                prompt_sha256=row[9],
                error_type=row[10],
            )
            for row in rows
        )

    def summarize(self, task: str) -> tuple[ModelTaskStats, ...]:
        """Aggregate reliability, output quality, latency, and cost by model."""
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                """
                SELECT model,
                       COUNT(*) AS samples,
                       SUM(status = 'succeeded') AS successes,
                       SUM(status = 'invalid_response') AS invalid_responses,
                       SUM(status = 'provider_error') AS provider_errors,
                       AVG(latency_seconds) AS average_latency_seconds,
                       AVG(cost_usd) AS average_cost_usd
                FROM llm_calls
                WHERE task = ?
                GROUP BY model
                ORDER BY model
                """,
                (task,),
            ).fetchall()
        return tuple(
            ModelTaskStats(
                model=row[0],
                samples=row[1],
                successes=row[2],
                invalid_responses=row[3],
                provider_errors=row[4],
                average_latency_seconds=row[5],
                average_cost_usd=row[6],
            )
            for row in rows
        )


class JSONTransport(Protocol):
    """Minimal injectable JSON-over-HTTP transport."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Send one request and decode one JSON object."""


class UrllibJSONTransport:
    """Standard-library HTTPS implementation."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Send JSON without logging headers or secret-bearing bodies."""
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(  # noqa: S310 - config restricts production to HTTPS
            url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                decoded = json.load(response)
        except urllib.error.HTTPError as error:
            message = f"OpenRouter HTTP error {error.code}"
            raise OpenRouterError(message) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            message = f"OpenRouter transport failed: {type(error).__name__}"
            raise OpenRouterError(message) from error
        if not isinstance(decoded, dict):
            message = "OpenRouter response must be a JSON object"
            raise OpenRouterError(message)
        return cast("dict[str, object]", decoded)


class OpenRouterClient:
    """Perform capability-routed, schema-validated, budgeted model calls."""

    def __init__(
        self,
        *,
        config: OpenRouterConfig,
        registry: ModelRegistry,
        transport: JSONTransport | None = None,
        telemetry: LLMTelemetry | None = None,
    ) -> None:
        """Inject all network and telemetry infrastructure."""
        self._config = config
        self._registry = registry
        self._transport = transport or UrllibJSONTransport()
        self._telemetry = telemetry or NullLLMTelemetry()

    def complete_structured(  # noqa: PLR0913 - explicit safety bounds belong at call sites
        self,
        *,
        task: str,
        messages: Sequence[ChatMessage],
        schema_name: str,
        schema: dict[str, object],
        budget: LLMBudget,
        min_context_tokens: int = 8_000,
        max_output_tokens: int = 1_000,
        max_attempts: int = 1,
        routing: ModelRoutingConstraints | None = None,
    ) -> StructuredCompletion:
        """Try policy-compliant models in adaptive order and validate their JSON response."""
        if max_attempts <= 0:
            message = "max_attempts must be greater than zero"
            raise ValueError(message)
        prompt = json.dumps([message.to_dict() for message in messages], sort_keys=True)
        requirements = ModelRequirements(
            min_context_tokens=min_context_tokens,
            structured_output=True,
            estimated_input_tokens=_estimate_tokens(prompt),
            max_output_tokens=max_output_tokens,
        )
        models = self._rank_models(task, requirements, routing)[:max_attempts]
        last_error: OpenRouterError | None = None
        for model in models:
            try:
                return self._complete_once(
                    task=task,
                    messages=messages,
                    schema_name=schema_name,
                    schema=schema,
                    budget=budget,
                    max_output_tokens=max_output_tokens,
                    prompt=prompt,
                    requirements=requirements,
                    model=model,
                )
            except BudgetExceededError:
                raise
            except OpenRouterError as error:
                last_error = error
        if last_error is not None:
            raise last_error
        message = "no model attempt was available"
        raise NoCompatibleModelError(message)

    def _rank_models(
        self,
        task: str,
        requirements: ModelRequirements,
        routing: ModelRoutingConstraints | None,
    ) -> tuple[ModelInfo, ...]:
        """Apply hard review constraints before task-specific adaptive ranking."""
        if routing is not None and routing.reviewer_author_model_id is not None:
            models = self._registry.rank_reviewers(requirements)
            author = routing.reviewer_author_model_id
            allowed = set(self._config.reviewer_model_ids)
            models = tuple(
                model for model in models if model.model_id in allowed and model.model_id != author
            )
            if routing.critical_review and self._config.critical_review_different_provider:
                author_provider = _model_provider(author)
                models = tuple(
                    model for model in models if _model_provider(model.model_id) != author_provider
                )
            if not models:
                message = "no independent reviewer model remains in the configured allowlist"
                raise NoCompatibleModelError(message)
        else:
            models = self._registry.rank(requirements)
        stats = {
            summary.model: summary
            for summary in self._telemetry.summarize(task)
            if summary.samples >= self._config.adaptive_min_samples
        }
        return tuple(
            sorted(
                models,
                key=lambda model: _adaptive_rank_key(
                    model,
                    requirements,
                    stats.get(model.model_id),
                ),
            ),
        )

    def _complete_once(  # noqa: PLR0913 - isolated audited network attempt
        self,
        *,
        task: str,
        messages: Sequence[ChatMessage],
        schema_name: str,
        schema: dict[str, object],
        budget: LLMBudget,
        max_output_tokens: int,
        prompt: str,
        requirements: ModelRequirements,
        model: ModelInfo,
    ) -> StructuredCompletion:
        """Execute and measure one model attempt."""
        estimate = model.estimated_cost(requirements)
        budget.begin_call(estimate)
        started_at = datetime.now(UTC)
        started = time.monotonic()
        usage = LLMUsage(0, 0, 0.0)
        call_id = ""
        status: LLMCallStatus = "provider_error"
        error_type: str | None = None
        try:
            response = self._transport.request(
                "POST",
                f"{self._config.base_url}/chat/completions",
                headers=_headers(self._config),
                payload={
                    "model": model.model_id,
                    "messages": [message.to_dict() for message in messages],
                    "max_tokens": max_output_tokens,
                    "temperature": 0,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                    },
                },
                timeout_seconds=self._config.timeout_seconds,
            )
            call_id, content, usage = _parse_completion(response, model)
            data = _validate_content(content, schema)
            status = "succeeded"
            return StructuredCompletion(
                call_id=call_id,
                model=model.model_id,
                data=data,
                usage=usage,
                latency_seconds=time.monotonic() - started,
            )
        except StructuredOutputError:
            status = "invalid_response"
            error_type = "StructuredOutputError"
            raise
        except OpenRouterError as error:
            error_type = type(error).__name__
            raise
        finally:
            latency = time.monotonic() - started
            budget.finish_call(estimate, usage.cost_usd)
            self._telemetry.record(
                LLMCallRecord(
                    call_id=call_id or _local_call_id(started_at, prompt),
                    task=task,
                    model=model.model_id,
                    status=status,
                    started_at=started_at,
                    latency_seconds=latency,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cost_usd=usage.cost_usd,
                    prompt_sha256=sha256(prompt.encode()).hexdigest(),
                    error_type=error_type,
                ),
            )


def fetch_model_registry(
    config: OpenRouterConfig,
    *,
    transport: JSONTransport | None = None,
) -> ModelRegistry:
    """Fetch and reference-filter the current model inventory."""
    active_transport = transport or UrllibJSONTransport()
    response = active_transport.request(
        "GET",
        f"{config.base_url}/models",
        headers=_headers(config),
        payload=None,
        timeout_seconds=config.timeout_seconds,
    )
    registry = ModelRegistry.from_api_response(response)
    return registry.apply_policy(config.selection_policy)


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse the small dotenv subset needed for local credentials."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        message = f"cannot read OpenRouter environment file {path}"
        raise ConfigurationError(message) from error
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.removeprefix("export ").split("=", maxsplit=1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def _environment_float(values: Mapping[str, str], name: str, default: float) -> float:
    """Read one finite numeric environment setting."""
    raw = values.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        message = f"{name} must be numeric"
        raise ConfigurationError(message) from error
    if not math.isfinite(value):
        message = f"{name} must be finite"
        raise ConfigurationError(message)
    return value


def _environment_int(values: Mapping[str, str], name: str, default: int) -> int:
    """Read one integer environment setting."""
    raw = values.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        message = f"{name} must be an integer"
        raise ConfigurationError(message) from error


def _environment_bool(values: Mapping[str, str], name: str, *, default: bool) -> bool:
    """Read one explicit boolean environment setting."""
    raw = values.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    message = f"{name} must be true or false"
    raise ConfigurationError(message)


def _environment_list(
    values: Mapping[str, str],
    name: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    """Read a comma-separated list without accepting empty entries."""
    raw = values.get(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_model(value: object) -> ModelInfo | None:
    """Parse one model entry, skipping incomplete or malformed records."""
    if not isinstance(value, dict):
        return None
    model_id = value.get("id")
    context_length = value.get("context_length")
    pricing = value.get("pricing")
    parameters = value.get("supported_parameters")
    if (
        not isinstance(model_id, str)
        or not isinstance(context_length, int)
        or not isinstance(pricing, dict)
        or not isinstance(parameters, list)
        or any(not isinstance(parameter, str) for parameter in parameters)
    ):
        return None
    try:
        prompt_price = float(pricing["prompt"])
        completion_price = float(pricing["completion"])
    except (KeyError, TypeError, ValueError):
        return None
    if prompt_price < 0 or completion_price < 0:
        return None
    knowledge_cutoff = _optional_api_date(value, "knowledge_cutoff")
    expiration_date = _optional_api_date(value, "expiration_date")
    if knowledge_cutoff is _INVALID_DATE or expiration_date is _INVALID_DATE:
        return None
    return ModelInfo(
        model_id=model_id,
        context_length=context_length,
        prompt_price_per_token=prompt_price,
        completion_price_per_token=completion_price,
        supported_parameters=frozenset(parameters),
        knowledge_cutoff=cast("date | None", knowledge_cutoff),
        expiration_date=cast("date | None", expiration_date),
        coding_index=_coding_index(value.get("benchmarks")),
    )


_INVALID_DATE = object()


def _optional_api_date(value: Mapping[str, object], key: str) -> date | None | object:
    """Parse an absent, null, ISO month, date, or datetime field."""
    raw = value.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return _INVALID_DATE
    normalized = raw.strip()
    try:
        if len(normalized) == _ISO_MONTH_LENGTH:
            return date.fromisoformat(f"{normalized}-01")
        if len(normalized) == _ISO_DATE_LENGTH:
            return date.fromisoformat(normalized)
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return _INVALID_DATE


def _coding_index(value: object) -> float | None:
    """Parse the documented Artificial Analysis coding index when present."""
    if not isinstance(value, dict):
        return None
    artificial_analysis = value.get("artificial_analysis")
    if not isinstance(artificial_analysis, dict):
        return None
    score = artificial_analysis.get("coding_index")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    parsed = float(score)
    return parsed if 0 <= parsed <= _PERCENT_MAX else None


def _reference_policy(
    models: tuple[ModelInfo, ...],
    policy: ModelSelectionPolicy,
    today: date,
) -> tuple[ModelInfo, bool]:
    """Resolve the configured reference and decide benchmark applicability."""
    reference = next(
        (model for model in models if model.model_id == policy.reference_model_id),
        None,
    )
    if reference is None or reference.knowledge_cutoff is None:
        message = (
            f"reference model {policy.reference_model_id!r} is absent or incomplete "
            "in the OpenRouter registry"
        )
        raise ConfigurationError(message)
    if reference.expiration_date is not None and reference.expiration_date <= today:
        message = f"reference model {policy.reference_model_id!r} is expired"
        raise ConfigurationError(message)
    scored_count = sum(model.coding_index is not None for model in models)
    coding_enabled = reference.coding_index is not None and scored_count >= _MIN_CODING_COVERAGE
    return reference, coding_enabled


@dataclass(frozen=True, slots=True)
class _ResolvedPolicy:
    """Resolved reference facts used by one registry-filter pass."""

    reference: ModelInfo
    today: date
    coding_enabled: bool
    coding_floor: float


def _meets_reference_policy(model: ModelInfo, resolved: _ResolvedPolicy) -> bool:
    """Return whether one model clears every enabled reference gate."""
    return (
        model.context_length >= resolved.reference.context_length
        and model.knowledge_cutoff is not None
        and resolved.reference.knowledge_cutoff is not None
        and model.knowledge_cutoff >= resolved.reference.knowledge_cutoff
        and (model.expiration_date is None or model.expiration_date > resolved.today)
        and (
            not resolved.coding_enabled
            or (model.coding_index is not None and model.coding_index >= resolved.coding_floor)
        )
    )


def _adaptive_rank_key(
    model: ModelInfo,
    requirements: ModelRequirements,
    stats: ModelTaskStats | None,
) -> tuple[float, float, float, float, str]:
    """Rank learned quality and reliability before latency and current cost."""
    if stats is None:
        quality_rate = 0.5
        reliability_rate = 0.5
        latency_seconds = 0.0
    else:
        quality_rate = stats.quality_rate
        reliability_rate = stats.reliability_rate
        latency_seconds = stats.average_latency_seconds
    return (
        -quality_rate,
        -reliability_rate,
        latency_seconds,
        model.estimated_cost(requirements),
        model.model_id,
    )


def _model_provider(model_id: str) -> str:
    """Return the canonical provider prefix used by OpenRouter model IDs."""
    return model_id.partition("/")[0]


def _headers(config: OpenRouterConfig) -> dict[str, str]:
    """Build authentication and optional attribution headers."""
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "X-Title": config.app_name,
    }
    if config.site_url:
        headers["HTTP-Referer"] = config.site_url
    return headers


def _estimate_tokens(text: str) -> int:
    """Use a conservative tokenizer-independent character estimate."""
    return max(1, (len(text) + 2) // 3)


def _parse_completion(
    response: Mapping[str, object],
    model: ModelInfo,
) -> tuple[str, str, LLMUsage]:
    """Extract the first text choice and provider usage."""
    call_id = response.get("id")
    choices = response.get("choices")
    usage_data = response.get("usage")
    if (
        not isinstance(call_id, str)
        or not isinstance(choices, list)
        or not choices
        or not isinstance(choices[0], dict)
        or not isinstance(usage_data, dict)
    ):
        message = "OpenRouter completion is missing id, choice, or usage"
        raise StructuredOutputError(message)
    message_data = choices[0].get("message")
    content = message_data.get("content") if isinstance(message_data, dict) else None
    prompt_tokens = usage_data.get("prompt_tokens")
    completion_tokens = usage_data.get("completion_tokens")
    if (
        not isinstance(content, str)
        or not isinstance(prompt_tokens, int)
        or not isinstance(completion_tokens, int)
    ):
        message = "OpenRouter completion contains invalid content or token usage"
        raise StructuredOutputError(message)
    usage = LLMUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=model.actual_cost(prompt_tokens, completion_tokens),
    )
    return call_id, content, usage


def _validate_content(content: str, schema: dict[str, object]) -> dict[str, object]:
    """Decode and locally validate provider structured output."""
    try:
        data = json.loads(content)
        jsonschema.validate(data, schema)
    except (json.JSONDecodeError, jsonschema.ValidationError, jsonschema.SchemaError) as error:
        message = f"model output failed JSON Schema validation: {type(error).__name__}"
        raise StructuredOutputError(message) from error
    if not isinstance(data, dict):
        message = "structured model output must be a JSON object"
        raise StructuredOutputError(message)
    return cast("dict[str, object]", data)


def _local_call_id(started_at: datetime, prompt: str) -> str:
    """Create a stable local identifier when the provider returns no ID."""
    raw = f"{started_at.isoformat()}:{sha256(prompt.encode()).hexdigest()}"
    return f"local-{sha256(raw.encode()).hexdigest()[:24]}"

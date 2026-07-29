"""Bounded structured OpenRouter calls with capability routing and telemetry."""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

import jsonschema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_API_BASE_URL = "https://openrouter.ai/api/v1"
_KEY_NAMES = ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")


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

    def __post_init__(self) -> None:
        """Reject unusable configuration."""
        if not self.api_key.strip():
            message = "OpenRouter API key must not be empty"
            raise ConfigurationError(message)
        if self.timeout_seconds <= 0:
            message = "OpenRouter timeout must be greater than zero"
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
        return cls(api_key=api_key)


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
class ModelInfo:
    """Routing facts for one OpenRouter model."""

    model_id: str
    context_length: int
    prompt_price_per_token: float
    completion_price_per_token: float
    supported_parameters: frozenset[str]

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

    def rank(self, requirements: ModelRequirements) -> tuple[ModelInfo, ...]:
        """Return all compatible models ordered by estimated cost and identifier."""
        compatible = tuple(model for model in self.models if model.supports(requirements))
        if not compatible:
            message = "no model satisfies context and structured-output requirements"
            raise NoCompatibleModelError(message)
        return tuple(sorted(
            compatible,
            key=lambda model: (model.estimated_cost(requirements), model.model_id),
        ))


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
            message = (
                f"estimated LLM cost ${projected:.6f} exceeds "
                f"budget ${self.max_cost_usd:.6f}"
            )
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


class LLMTelemetry(Protocol):
    """Destination for secret-free LLM call measurements."""

    def record(self, call: LLMCallRecord) -> None:
        """Persist one completed call."""


class NullLLMTelemetry:
    """Discard telemetry for callers that explicitly provide no store."""

    def record(self, call: LLMCallRecord) -> None:
        """Discard one record."""
        del call


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
    ) -> StructuredCompletion:
        """Try compatible models in cost order and validate their JSON response."""
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
        models = self._registry.rank(requirements)[:max_attempts]
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
    """Fetch the current model inventory through the injectable transport."""
    active_transport = transport or UrllibJSONTransport()
    response = active_transport.request(
        "GET",
        f"{config.base_url}/models",
        headers=_headers(config),
        payload=None,
        timeout_seconds=config.timeout_seconds,
    )
    return ModelRegistry.from_api_response(response)


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
    return ModelInfo(
        model_id=model_id,
        context_length=context_length,
        prompt_price_per_token=prompt_price,
        completion_price_per_token=completion_price,
        supported_parameters=frozenset(parameters),
    )


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

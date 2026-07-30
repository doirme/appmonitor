"""Tests for the bounded OpenRouter foundation."""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from appmonitor.openrouter import (
    BudgetExceededError,
    ChatMessage,
    ConfigurationError,
    LLMBudget,
    LLMCallRecord,
    ModelRegistry,
    ModelRequirements,
    ModelRoutingConstraints,
    ModelSelectionPolicy,
    NoCompatibleModelError,
    OpenRouterClient,
    OpenRouterConfig,
    SQLiteLLMTelemetry,
    StructuredOutputError,
    fetch_model_registry,
)

_EXPECTED_TOTAL_TOKENS = 15
_SHA256_HEX_LENGTH = 64
_REFERENCE_CONTEXT = 32_000
_REFERENCE_CODING_INDEX = 30.4
_CUSTOM_CODING_INDEX = 45.0
_CUSTOM_ADAPTIVE_MIN_SAMPLES = 5


class FakeTransport:
    """Return fixed JSON payloads and retain requests for assertions."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        """Store ordered responses."""
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object] | None,
        timeout_seconds: float,
    ) -> dict[str, object]:
        """Record a sanitized request shape and return the next response."""
        self.requests.append(
            {
                "method": method,
                "url": url,
                "authorization": headers["Authorization"],
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            },
        )
        return self.responses.pop(0)


def _models_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "expensive/model",
                "context_length": 100_000,
                "pricing": {"prompt": "0.00001", "completion": "0.00002"},
                "supported_parameters": ["structured_outputs"],
            },
            {
                "id": "cheap/model",
                "context_length": 32_000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "supported_parameters": ["structured_outputs"],
            },
            {
                "id": "plain/model",
                "context_length": 64_000,
                "pricing": {"prompt": "0", "completion": "0"},
                "supported_parameters": ["temperature"],
            },
        ],
    }


def _completion(content: str = '{"summary":"ok"}') -> dict[str, object]:
    return {
        "id": "generation-1",
        "model": "cheap/model",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _requested_model(transport: FakeTransport) -> str:
    """Return the model ID from the first retained request."""
    payload = transport.requests[0]["payload"]
    assert isinstance(payload, dict)
    model = payload["model"]
    assert isinstance(model, str)
    return model


def _reference_model(**overrides: object) -> dict[str, object]:
    model: dict[str, object] = {
        "id": "openai/gpt-oss-120b",
        "context_length": _REFERENCE_CONTEXT,
        "knowledge_cutoff": "2024-06-30",
        "expiration_date": None,
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        "supported_parameters": ["structured_outputs"],
        "benchmarks": {"artificial_analysis": {"coding_index": _REFERENCE_CODING_INDEX}},
    }
    model.update(overrides)
    return model


def _candidate(  # noqa: PLR0913 - model fixtures expose independent malformed fields
    model_id: str,
    *,
    context: int = 64_000,
    cutoff: object = "2025-01-31",
    expiration: object = None,
    coding_index: object = 40.0,
    prompt_price: str = "0.0000001",
) -> dict[str, object]:
    benchmarks: dict[str, object] = {
        "artificial_analysis": {"coding_index": coding_index},
    }
    return {
        "id": model_id,
        "context_length": context,
        "knowledge_cutoff": cutoff,
        "expiration_date": expiration,
        "pricing": {"prompt": prompt_price, "completion": "0.0000002"},
        "supported_parameters": ["structured_outputs"],
        "benchmarks": benchmarks,
    }


def test_config_loads_conventional_and_legacy_key_names(tmp_path: Path) -> None:
    """The user-provided legacy variable works without exposing its value."""
    env_file = tmp_path / ".env.txt"
    env_file.write_text("OPEN_ROUTER_API_KEY=secret-value\n", encoding="utf-8")

    config = OpenRouterConfig.from_env_file(env_file)

    assert config.api_key == "secret-value"
    assert "secret-value" not in repr(config)


def test_config_loads_reference_and_thresholds_with_documented_defaults(tmp_path: Path) -> None:
    """Reference routing policy is configurable without hard-coding it in selection."""
    env_file = tmp_path / ".env.txt"
    env_file.write_text(
        "OPENROUTER_API_KEY=secret\n"
        "OPENROUTER_REFERENCE_MODEL=vendor/reference\n"
        "OPENROUTER_MIN_CODING_INDEX=45\n"
        "OPENROUTER_REVIEWER_MODELS=vendor/reviewer-a,vendor/reviewer-b\n"
        "OPENROUTER_CRITICAL_REVIEW_DIFFERENT_PROVIDER=false\n"
        "OPENROUTER_ADAPTIVE_MIN_SAMPLES=5\n",
        encoding="utf-8",
    )

    configured = OpenRouterConfig.from_env_file(env_file)
    defaults = OpenRouterConfig(api_key="secret")

    assert configured.reference_model_id == "vendor/reference"
    assert configured.min_coding_index == _CUSTOM_CODING_INDEX
    assert configured.reviewer_model_ids == ("vendor/reviewer-a", "vendor/reviewer-b")
    assert configured.critical_review_different_provider is False
    assert configured.adaptive_min_samples == _CUSTOM_ADAPTIVE_MIN_SAMPLES
    assert defaults.reference_model_id == "openai/gpt-oss-120b"
    assert defaults.min_coding_index == 0.0
    assert "moonshotai/kimi-k2.7-code" in defaults.reviewer_model_ids
    assert "qwen/qwen3-coder-next" in defaults.reviewer_model_ids
    assert defaults.critical_review_different_provider is True


@pytest.mark.parametrize(
    ("reference_model_id", "coding_index"),
    [
        ("", 0.0),
        ("reference/model", -1.0),
        ("reference/model", 101.0),
    ],
)
def test_config_rejects_invalid_reference_thresholds(
    reference_model_id: str,
    coding_index: float,
) -> None:
    """Invalid policy values fail at configuration rather than during routing."""
    with pytest.raises(ConfigurationError):
        OpenRouterConfig(
            api_key="secret",
            reference_model_id=reference_model_id,
            min_coding_index=coding_index,
        )


def test_config_rejects_non_numeric_environment_threshold(tmp_path: Path) -> None:
    """Malformed dotenv thresholds produce an actionable configuration error."""
    env_file = tmp_path / ".env.txt"
    env_file.write_text(
        "OPENROUTER_API_KEY=secret\nOPENROUTER_MIN_CODING_INDEX=often\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="must be numeric"):
        OpenRouterConfig.from_env_file(env_file)


def test_reference_policy_filters_before_cost_ranking() -> None:
    """Cheap candidates below reference quality limits are removed."""
    candidates = [
        _candidate("eligible/model"),
        _candidate("small/model", context=16_000, prompt_price="0"),
        _candidate("old/model", cutoff="2024-01-01", prompt_price="0"),
        _candidate("expired/model", expiration="2026-01-01", prompt_price="0"),
        _candidate("weak/model", coding_index=20.0, prompt_price="0"),
        *[_candidate(f"scored/model-{index}", prompt_price="0.00001") for index in range(5)],
    ]
    registry = ModelRegistry.from_api_response(
        {"data": [_reference_model(), *candidates]},
    ).apply_policy(
        ModelSelectionPolicy(
            reference_model_id="openai/gpt-oss-120b",
            min_coding_index=0,
        ),
        today=date(2026, 7, 30),
    )

    ranked = registry.rank(ModelRequirements())

    assert ranked[0].model_id == "eligible/model"
    assert {model.model_id for model in ranked}.isdisjoint(
        {"small/model", "old/model", "expired/model", "weak/model"},
    )


def test_coding_filter_is_ignored_when_fewer_than_ten_models_have_scores() -> None:
    """Sparse benchmark coverage cannot accidentally collapse the registry."""
    payload = {
        "data": [
            _reference_model(),
            _candidate("unscored/model", coding_index=None),
        ],
    }
    registry = ModelRegistry.from_api_response(payload).apply_policy(
        ModelSelectionPolicy(),
        today=date(2026, 7, 30),
    )

    assert registry.select(ModelRequirements()).model_id == "unscored/model"


@pytest.mark.parametrize(
    "reference",
    [
        None,
        _reference_model(knowledge_cutoff=None),
        _reference_model(context_length=None),
    ],
)
def test_reference_model_must_be_present_and_complete(reference: object) -> None:
    """An absent or unusable reference produces an explicit configuration error."""
    models = [_candidate("candidate/model")]
    if isinstance(reference, dict):
        models.insert(0, reference)

    with pytest.raises(ConfigurationError, match="reference model"):
        ModelRegistry.from_api_response({"data": models}).apply_policy(
            ModelSelectionPolicy(),
            today=date(2026, 7, 30),
        )


def test_fetch_registry_uses_models_api_without_endpoint_uptime() -> None:
    """Registry filtering needs only the documented models JSON response."""
    transport = FakeTransport(
        [
            {
                "data": [
                    _reference_model(),
                    _candidate("eligible/model", coding_index=None),
                    _candidate("reviewer/model", context=16_000, coding_index=None),
                ],
            },
        ],
    )

    registry = fetch_model_registry(
        OpenRouterConfig(api_key="secret", reviewer_model_ids=("reviewer/model",)),
        transport=transport,
    )

    assert registry.select(ModelRequirements()).model_id == "eligible/model"
    assert "reviewer/model" in {
        model.model_id for model in registry.rank_reviewers(ModelRequirements())
    }
    assert [request["url"] for request in transport.requests] == [
        "https://openrouter.ai/api/v1/models",
    ]


def test_registry_skips_models_with_malformed_dates() -> None:
    """Unknown date formats are not introduced into the internal model shape."""
    registry = ModelRegistry.from_api_response(
        {
            "data": [
                _reference_model(),
                _candidate("malformed/model", cutoff="not-a-date"),
            ],
        },
    )

    assert [model.model_id for model in registry.models] == ["openai/gpt-oss-120b"]


def test_registry_routes_to_cheapest_compatible_model() -> None:
    """Routing filters capabilities and context before comparing cost."""
    registry = ModelRegistry.from_api_response(_models_payload())

    model = registry.select(
        ModelRequirements(
            min_context_tokens=16_000,
            structured_output=True,
            estimated_input_tokens=1_000,
            max_output_tokens=500,
        ),
    )

    assert model.model_id == "cheap/model"


def test_registry_skips_negative_sentinel_prices() -> None:
    """Dynamic routers with unknown negative prices cannot bypass cost controls."""
    payload = _models_payload()
    models = payload["data"]
    assert isinstance(models, list)
    models.append(
        {
            "id": "dynamic/router",
            "context_length": 100_000,
            "pricing": {"prompt": "-1", "completion": "-1"},
            "supported_parameters": ["structured_outputs"],
        },
    )

    registry = ModelRegistry.from_api_response(payload)

    assert {model.model_id for model in registry.models} == {
        "expensive/model",
        "cheap/model",
        "plain/model",
    }


def test_client_sends_schema_validates_response_and_records_telemetry(tmp_path: Path) -> None:
    """One structured call is bounded, validated, and durably measured."""
    transport = FakeTransport([_completion()])
    telemetry = SQLiteLLMTelemetry(tmp_path / "telemetry.sqlite3")
    client = OpenRouterClient(
        config=OpenRouterConfig(api_key="secret"),
        registry=ModelRegistry.from_api_response(_models_payload()),
        transport=transport,
        telemetry=telemetry,
    )
    budget = LLMBudget(max_calls=1, max_cost_usd=0.01)
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }

    result = client.complete_structured(
        task="smoke_test",
        messages=(ChatMessage("user", "Return a short status."),),
        schema_name="status",
        schema=schema,
        budget=budget,
        max_output_tokens=50,
    )

    assert result.data == {"summary": "ok"}
    assert result.usage.total_tokens == _EXPECTED_TOTAL_TOKENS
    request_payload = transport.requests[0]["payload"]
    assert isinstance(request_payload, dict)
    assert request_payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "status", "strict": True, "schema": schema},
    }
    with sqlite3.connect(telemetry.database) as connection:
        row = connection.execute(
            "SELECT task, model, status, prompt_sha256 FROM llm_calls",
        ).fetchone()
    assert row[:3] == ("smoke_test", "cheap/model", "succeeded")
    assert len(row[3]) == _SHA256_HEX_LENGTH


def test_budget_rejects_call_before_transport() -> None:
    """An unaffordable estimated call never reaches OpenRouter."""
    transport = FakeTransport([_completion()])
    client = OpenRouterClient(
        config=OpenRouterConfig(api_key="secret"),
        registry=ModelRegistry.from_api_response(_models_payload()),
        transport=transport,
    )

    with pytest.raises(BudgetExceededError):
        client.complete_structured(
            task="too_expensive",
            messages=(ChatMessage("user", "x" * 1000),),
            schema_name="status",
            schema={"type": "object"},
            budget=LLMBudget(max_calls=1, max_cost_usd=0.000001),
            max_output_tokens=500,
        )

    assert transport.requests == []


def test_invalid_structured_output_is_rejected_and_measured(tmp_path: Path) -> None:
    """Valid JSON that violates the requested schema is not returned to agents."""
    telemetry = SQLiteLLMTelemetry(tmp_path / "telemetry.sqlite3")
    client = OpenRouterClient(
        config=OpenRouterConfig(api_key="secret"),
        registry=ModelRegistry.from_api_response(_models_payload()),
        transport=FakeTransport([_completion('{"unexpected":true}')]),
        telemetry=telemetry,
    )

    with pytest.raises(StructuredOutputError):
        client.complete_structured(
            task="invalid",
            messages=(ChatMessage("user", "status"),),
            schema_name="status",
            schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
            budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
        )

    assert telemetry.list_calls()[0].status == "invalid_response"


def test_client_falls_back_to_next_ranked_model(tmp_path: Path) -> None:
    """A bounded retry uses the next compatible model after an invalid provider response."""
    telemetry = SQLiteLLMTelemetry(tmp_path / "telemetry.sqlite3")
    transport = FakeTransport(
        [
            {"id": "bad-generation", "choices": [], "usage": {}},
            {
                **_completion(),
                "id": "good-generation",
                "model": "expensive/model",
            },
        ],
    )
    client = OpenRouterClient(
        config=OpenRouterConfig(api_key="secret"),
        registry=ModelRegistry.from_api_response(_models_payload()),
        transport=transport,
        telemetry=telemetry,
    )

    result = client.complete_structured(
        task="fallback",
        messages=(ChatMessage("user", "status"),),
        schema_name="status",
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
        budget=LLMBudget(max_calls=2, max_cost_usd=0.1),
        max_attempts=2,
    )

    assert result.call_id == "good-generation"
    payloads = [request["payload"] for request in transport.requests]
    assert [payload["model"] for payload in payloads if isinstance(payload, dict)] == [
        "cheap/model",
        "expensive/model",
    ]
    assert [call.status for call in telemetry.list_calls()] == [
        "invalid_response",
        "succeeded",
    ]


def test_client_adapts_ranking_from_task_specific_telemetry(tmp_path: Path) -> None:
    """Repeated valid task results outrank a cheaper model with provider failures."""
    telemetry = SQLiteLLMTelemetry(tmp_path / "telemetry.sqlite3")
    started_at = datetime(2026, 7, 30, tzinfo=UTC)
    for index in range(3):
        telemetry.record(
            LLMCallRecord(
                call_id=f"cheap-{index}",
                task="patch_implementer",
                model="cheap/model",
                status="provider_error",
                started_at=started_at,
                latency_seconds=0.1,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0,
                prompt_sha256="0" * _SHA256_HEX_LENGTH,
                error_type="OpenRouterError",
            ),
        )
        telemetry.record(
            LLMCallRecord(
                call_id=f"expensive-{index}",
                task="patch_implementer",
                model="expensive/model",
                status="succeeded",
                started_at=started_at,
                latency_seconds=0.2,
                prompt_tokens=10,
                completion_tokens=5,
                cost_usd=0.001,
                prompt_sha256="1" * _SHA256_HEX_LENGTH,
            ),
        )
    transport = FakeTransport([_completion()])
    client = OpenRouterClient(
        config=OpenRouterConfig(api_key="secret"),
        registry=ModelRegistry.from_api_response(_models_payload()),
        transport=transport,
        telemetry=telemetry,
    )

    result = client.complete_structured(
        task="patch_implementer",
        messages=(ChatMessage("user", "status"),),
        schema_name="status",
        schema={"type": "object"},
        budget=LLMBudget(max_calls=1, max_cost_usd=0.1),
    )

    assert result.model == "expensive/model"
    assert _requested_model(transport) == "expensive/model"


def test_reviewer_must_use_an_allowed_model_distinct_from_author() -> None:
    """Reviewer independence is enforced before adaptive or cost ranking."""
    payload = {
        "data": [
            _candidate("openai/author", prompt_price="0"),
            _candidate("anthropic/reviewer", prompt_price="0.00001"),
        ],
    }
    transport = FakeTransport([_completion()])
    client = OpenRouterClient(
        config=OpenRouterConfig(
            api_key="secret",
            reviewer_model_ids=("openai/author", "anthropic/reviewer"),
        ),
        registry=ModelRegistry.from_api_response(payload),
        transport=transport,
    )

    result = client.complete_structured(
        task="patch_reviewer",
        messages=(ChatMessage("user", "review"),),
        schema_name="review",
        schema={"type": "object"},
        budget=LLMBudget(max_calls=1, max_cost_usd=0.1),
        routing=ModelRoutingConstraints.reviewer(author_model_id="openai/author"),
    )

    assert result.model == "anthropic/reviewer"
    assert _requested_model(transport) == "anthropic/reviewer"


def test_critical_reviewer_must_also_use_a_different_provider() -> None:
    """Critical review can exclude the author's complete provider family."""
    payload = {
        "data": [
            _candidate("openai/author", prompt_price="0"),
            _candidate("openai/reviewer", prompt_price="0.000001"),
            _candidate("anthropic/reviewer", prompt_price="0.00001"),
        ],
    }
    transport = FakeTransport([_completion()])
    client = OpenRouterClient(
        config=OpenRouterConfig(
            api_key="secret",
            reviewer_model_ids=("openai/reviewer", "anthropic/reviewer"),
        ),
        registry=ModelRegistry.from_api_response(payload),
        transport=transport,
    )

    client.complete_structured(
        task="patch_reviewer",
        messages=(ChatMessage("user", "review"),),
        schema_name="review",
        schema={"type": "object"},
        budget=LLMBudget(max_calls=1, max_cost_usd=0.1),
        routing=ModelRoutingConstraints.reviewer(
            author_model_id="openai/author",
            critical=True,
        ),
    )

    assert _requested_model(transport) == "anthropic/reviewer"


def test_critical_provider_separation_can_be_disabled() -> None:
    """The explicit false flag keeps same-provider reviewers admissible."""
    payload = {
        "data": [
            _candidate("openai/author", prompt_price="0"),
            _candidate("openai/reviewer", prompt_price="0.000001"),
            _candidate("anthropic/reviewer", prompt_price="0.00001"),
        ],
    }
    transport = FakeTransport([_completion()])
    client = OpenRouterClient(
        config=OpenRouterConfig(
            api_key="secret",
            reviewer_model_ids=("openai/reviewer", "anthropic/reviewer"),
            critical_review_different_provider=False,
        ),
        registry=ModelRegistry.from_api_response(payload),
        transport=transport,
    )

    client.complete_structured(
        task="patch_reviewer",
        messages=(ChatMessage("user", "review"),),
        schema_name="review",
        schema={"type": "object"},
        budget=LLMBudget(max_calls=1, max_cost_usd=0.1),
        routing=ModelRoutingConstraints.reviewer(
            author_model_id="openai/author",
            critical=True,
        ),
    )

    assert _requested_model(transport) == "openai/reviewer"


def test_reviewer_fails_closed_without_an_independent_allowed_model() -> None:
    """Registry changes cannot relax reviewer independence."""
    client = OpenRouterClient(
        config=OpenRouterConfig(
            api_key="secret",
            reviewer_model_ids=("openai/author",),
        ),
        registry=ModelRegistry.from_api_response(
            {"data": [_candidate("openai/author")]},
        ),
        transport=FakeTransport([]),
    )

    with pytest.raises(NoCompatibleModelError, match="independent reviewer"):
        client.complete_structured(
            task="patch_reviewer",
            messages=(ChatMessage("user", "review"),),
            schema_name="review",
            schema={"type": "object"},
            budget=LLMBudget(max_calls=1, max_cost_usd=0.1),
            routing=ModelRoutingConstraints.reviewer(author_model_id="openai/author"),
        )

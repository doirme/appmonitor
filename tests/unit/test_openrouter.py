"""Tests for the bounded OpenRouter foundation."""

import sqlite3
from pathlib import Path

import pytest

from appmonitor.openrouter import (
    BudgetExceededError,
    ChatMessage,
    LLMBudget,
    ModelRegistry,
    ModelRequirements,
    OpenRouterClient,
    OpenRouterConfig,
    SQLiteLLMTelemetry,
    StructuredOutputError,
)

_EXPECTED_TOTAL_TOKENS = 15
_SHA256_HEX_LENGTH = 64


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


def test_config_loads_conventional_and_legacy_key_names(tmp_path: Path) -> None:
    """The user-provided legacy variable works without exposing its value."""
    env_file = tmp_path / ".env.txt"
    env_file.write_text("OPEN_ROUTER_API_KEY=secret-value\n", encoding="utf-8")

    config = OpenRouterConfig.from_env_file(env_file)

    assert config.api_key == "secret-value"
    assert "secret-value" not in repr(config)


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

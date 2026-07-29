# OpenRouter foundation

AppMonitor treats an LLM as an untrusted structured recommendation service. The deterministic
orchestrator owns budgets, routing, schema validation, persistence, and every state change.

## Credential file

The loader accepts the conventional variable and the existing local alias:

```dotenv
OPENROUTER_API_KEY=...
```

or:

```dotenv
OPEN_ROUTER_API_KEY=...
```

Load the current local file without adding its value to the process environment:

```python
from pathlib import Path

from appmonitor import OpenRouterConfig

config = OpenRouterConfig.from_env_file(Path(".env.txt"))
```

`OpenRouterConfig` excludes the key from `repr`. Files matching `.env` and `.env.*` are ignored by
Git and AppMonitor artifact snapshots.

## Model discovery and routing

```python
from appmonitor import ModelRequirements, fetch_model_registry

registry = fetch_model_registry(config)
model = registry.select(
    ModelRequirements(
        min_context_tokens=8_000,
        structured_output=True,
        estimated_input_tokens=500,
        max_output_tokens=300,
    )
)
```

The registry reads model context length, supported parameters, and per-token prices from
OpenRouter. Selection first rejects incompatible models, models with malformed pricing, and
dynamic routers that use negative price sentinels. It then selects the least expensive compatible
model with the model ID as a stable tie-breaker.

## Bounded structured call

```python
from appmonitor import ChatMessage, LLMBudget, OpenRouterClient

schema = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
client = OpenRouterClient(config=config, registry=registry)
result = client.complete_structured(
    task="run_assessment",
    messages=(ChatMessage("user", "Summarize the supplied facts."),),
    schema_name="run_assessment",
    schema=schema,
    budget=LLMBudget(max_calls=1, max_cost_usd=0.01),
    max_output_tokens=300,
)
```

Before network access, the client conservatively estimates input tokens and reserves the model's
maximum estimated cost. `BudgetExceededError` prevents the request when either the call or cost
limit would be exceeded.

The request uses OpenRouter's `response_format.type=json_schema`. The returned text is decoded and
validated again locally with `jsonschema`. Invalid JSON, missing fields, extra fields prohibited by
the schema, or incorrect types raise `StructuredOutputError`.

## Telemetry

Pass `SQLiteLLMTelemetry(".appmonitor/openrouter.sqlite3")` to the client for durable measurements.
The `llm_calls` table records:

- provider or local call ID;
- task and selected model;
- status and sanitized error type;
- timestamp and latency;
- input/output tokens and calculated USD cost;
- SHA-256 of the serialized messages.

It deliberately stores no API key, prompt text, or response content.

## Real validation

On 2026-07-29, the local `.env.txt` key successfully completed a strict JSON Schema smoke test:

- model: `google/gemma-4-26b-a4b-it:free`;
- validated result: `{"status": "ok"}`;
- total tokens reported: 127;
- calculated model cost: USD 0;
- latency: 2.047 seconds.

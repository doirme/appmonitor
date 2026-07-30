# OpenRouter API

## Configuration

```python
config = OpenRouterConfig.from_env_file(".env.txt")
```

The loader accepts `OPENROUTER_API_KEY` and `OPEN_ROUTER_API_KEY`. The key is excluded from
`repr`. `OpenRouterConfig` also accepts `base_url`, `timeout_seconds`, `app_name`, and optional
`site_url`. Empty keys and non-positive timeouts raise `ConfigurationError`.

## Registry and routing

`fetch_model_registry(config, transport=None) -> ModelRegistry` performs one explicit `GET
/models`, statically narrows the catalog, and reads 30-minute availability from each remaining
model's official endpoints API. Live endpoint requests use at most eight threads. The registry is
not fetched inside `complete_structured`, refreshed automatically, or cached by the library.

`ModelRegistry.from_api_response(response)` retains entries with:

- a string model ID;
- an integer context length;
- string prompt and completion prices parseable as floats;
- a list of string supported parameters;
- non-negative prompt and completion prices.

The parser also accepts validated ISO `knowledge_cutoff` and `expiration_date` values and the
numeric `benchmarks.artificial_analysis.coding_index` in the range 0 to 100. Missing optional
benchmark fields become `None`; malformed dates cause that model entry to be skipped. Malformed
entries and negative sentinel prices are skipped.

### Reference policy

The default environment configuration is:

```dotenv
OPENROUTER_REFERENCE_MODEL=openai/gpt-oss-120b
OPENROUTER_MIN_AVAILABILITY=95
OPENROUTER_MIN_CODING_INDEX=0
```

Availability is a percentage from 0 to 100 over the OpenRouter endpoint API's rolling 30-minute
window. The coding index is the Artificial Analysis Coding Index expressed in points from 0 to
100. Before price ranking, a candidate must:

- have context length at least equal to the resolved reference;
- have a knowledge cutoff equal to or later than the reference;
- have no expiration date at or before the current UTC date;
- meet `OPENROUTER_MIN_AVAILABILITY`;
- meet both the reference coding index and `OPENROUTER_MIN_CODING_INDEX`.

The coding criterion is disabled when the reference has no coding index or fewer than ten parsed
models have one. A missing/incomplete/expired reference or missing reference availability raises
`ConfigurationError`; it never silently falls back to a hard-coded model. Missing candidate
metadata simply makes that candidate ineligible.

The fields come only from OpenRouter JSON APIs. AppMonitor does not scrape model pages.
The implemented shapes were checked against the official
[Models API schema](https://openrouter.ai/docs/guides/overview/models),
[model endpoints API](https://openrouter.ai/docs/api/api-reference/endpoints/list-endpoints), and
[benchmarks API](https://openrouter.ai/docs/api/api-reference/benchmarks/get-benchmarks).

`ModelRequirements` defaults to 8,000 context tokens, structured output, one estimated input
token, and 1,000 maximum output tokens. `rank(requirements)` first requires sufficient context and
either `structured_outputs` or `response_format` support. It then sorts by:

```text
estimated_input_tokens * prompt_price
+ max_output_tokens * completion_price
+ model_id as deterministic tie-breaker
```

The model ID is only the tie-breaker, not an additional monetary term. `select()` returns the first
ranked model.

## Budget

`LLMBudget(max_calls, max_cost_usd)` is mutable state shared by one workflow. `begin_call()` reserves
the estimated worst-case call cost before network access. `finish_call()` replaces the reservation
with cost calculated from provider-reported token usage.

`calls` counts attempted calls, including failed provider or invalid structured responses.
`spent_usd` reflects reported token usage. `BudgetExceededError` stops immediately; it does not
fall back to another model.

## Structured completions

```python
result = client.complete_structured(
    task="run_critic",
    messages=(ChatMessage("user", "Analyze these facts"),),
    schema_name="run_assessment",
    schema=schema,
    budget=budget,
    min_context_tokens=8_000,
    max_output_tokens=1_000,
    max_attempts=2,
)
```

The client estimates input tokens from serialized messages, ranks the injected registry, and tries
up to `max_attempts` different models. It sends temperature zero and strict JSON Schema response
formatting. Returned content is decoded and validated locally with `jsonschema`.

Provider/transport errors and `StructuredOutputError` advance to the next ranked model. A successful
`StructuredCompletion` contains `call_id`, `model`, validated `data`, `usage`, and
`latency_seconds`.

After reference filtering, cost remains the deterministic ordering. Invalid provider or schema
responses advance to the next eligible model within `max_attempts`. The current rank does not use
prior task-specific response quality or local historical cost.

## Telemetry

`SQLiteLLMTelemetry(path)` creates `llm_calls`. `record()` is used by the client after every
attempt. `list_calls()` returns insertion-ordered `LLMCallRecord` values with:

- call ID, task, model, status, and start time;
- latency, prompt tokens, completion tokens, and calculated cost;
- SHA-256 of serialized messages;
- sanitized error type.

Prompts, responses, authorization headers, and credentials are not stored. See
[model routing and observability](../tutorials/model-routing-and-observability.md) for reports that
can be built from these rows.

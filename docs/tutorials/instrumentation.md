# Instrument a Python function

Use instrumentation when function-level arguments, returns, exceptions, or business outputs add
useful context beyond process logs.

```python
from pathlib import Path

from appmonitor import (
    InMemoryCallRecorder,
    OutputArtifact,
    ResourceBudget,
    monitored,
)

repository = Path.cwd()
recorder = InMemoryCallRecorder()


@monitored(
    goal="Create the monthly forecast",
    outputs=(OutputArtifact("outputs/forecast_*.json"),),
    budget=ResourceBudget(max_runtime_seconds=30, max_memory_delta_mb=256),
    repository=repository,
    recorder=recorder,
)
def build_forecast(month: str, api_key: str) -> Path:
    output = repository / "outputs" / f"forecast_{month}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text('{"status": "ready"}', encoding="utf-8")
    return output


build_forecast("2026-07", api_key="secret")
observation = recorder.records[-1]
print(observation.outcome)
print(observation.output_checks)
print(observation.budget_checks)
```

The `api_key` value is redacted. The returned `Path` content is not persisted; AppMonitor stores
its type and a SHA-256 identity.

## Compare a later call

```python
from appmonitor import CallReference

reference = CallReference.from_observation(observation)
```

Pass `reference=reference` to a later `@monitored` declaration. The next observation reports
duration and memory ratios and whether the bounded return representation has the same hash.

## Persist observations

Replace the in-memory recorder with:

```python
from appmonitor import SQLiteInstrumentationStore

recorder = SQLiteInstrumentationStore(".appmonitor/runs.sqlite3")
```

Using the run database keeps function-level evidence beside process-level observations, although
instrumented calls are not automatically associated with a `run_id` in this version.

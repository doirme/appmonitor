# In-process instrumentation

Instrumentation is optional and complements subprocess monitoring. It can observe values inside a
Python call but cannot replace `RunClient` for import failures, child processes, or abrupt exits.

## `monitored`

```python
@monitored(
    *,
    goal: str,
    outputs: Sequence[OutputArtifact] = (),
    budget: ResourceBudget | None = None,
    repository: str | Path | None = None,
    recorder: CallRecorder | None = None,
    reference: CallReference | None = None,
)
```

The decorator records bound arguments, return identity or exception, duration, RSS variation,
artifact changes, output checks, resource checks, and an optional reference comparison. It returns
the original result and re-raises the original exception.

Values are represented as bounded strings. Arguments whose names contain `api_key`, `token`,
`password`, or `secret` are replaced with `<redacted>`. Common OpenRouter-style keys are redacted
from other captured values. Return values are retained only as type and SHA-256 of their bounded
representation.

Without a recorder, observations are discarded.

## Declarations

`OutputArtifact(pattern, required=True)` checks created and modified repository paths after the
call. It requires `repository` to be configured for meaningful results.

`ResourceBudget(max_runtime_seconds=None, max_memory_delta_mb=None)` creates deterministic checks.
Limits use seconds and mebibytes. A failed check is evidence and does not terminate or alter the
function call.

## Records and references

`CallObservation` contains call identity, function, goal, UTC start, duration, bounded arguments,
outcome, return hash, sanitized exception, RSS delta, artifacts, checks, and comparison.

`CallReference.from_observation(observation)` copies duration, RSS delta, and return hash.
Supplying it to a later decorator produces duration and positive-memory ratios plus return-hash
equality.

`InMemoryCallRecorder.records` returns observations in insertion order.
`SQLiteInstrumentationStore(path)` creates `instrumented_calls`; `record()` persists portable JSON
and `list_records()` reconstructs observations.

Instrumentation is synchronous. It does not yet sample peak memory inside a call or capture calls
that terminate the interpreter.

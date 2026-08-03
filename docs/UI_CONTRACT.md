# UI contract — the two surfaces the front end binds to

Both surfaces are produced by real runs. No value in either is seeded, synthesized, or
back-filled; if something did not happen, it does not appear.

---

## Surface A — metric series (queryable via the Grafana MCP)

Read through the Grafana MCP `query_prometheus` tool against datasource
`grafanacloud-prom`. Every diagnosis query MUST pin both `job` and `mode` — see
"Scoping" below; this is enforced in code by [agent/series_scope_guard.py](../agent/series_scope_guard.py).

### Instrumented layers

Exactly two layers are instrumented. **Audio description is not instrumented** — there is
no AD metric, and the agent is instructed to say it cannot assess AD rather than infer
anything about it. The UI must not render an AD health state.

| Layer | Metric | What it measures | Healthy | Faulted (real, measured) |
|---|---|---|---|---|
| `captions` | `caption_cue_sync_offset_seconds` | program clock − media timestamp of last published caption cue | ~0.50–0.60 s | climbs unbounded; observed to 125.9 s |
| `sign_language` | `feed_liveness_seconds` | seconds since the monitored feed process last produced a frame | ~0.27–0.30 s | climbs unbounded; observed to 270.9 s |

Companion flags: `caption_cue_publisher_stalled`, `feed_frozen` (both `0` healthy / `1` faulted).

> **`sign_language` is feed liveness on a stand-in feed, not a sign-language-content
> measurement.** There is no sign-language content in this repository; the monitored feed
> is a second process carrying the same program video. The `layer="sign_language"` label
> denotes which layer slot the stand-in occupies. No UI copy may describe it as a signer
> feed. See [scripts/feed_liveness_with_telemetry.py](../scripts/feed_liveness_with_telemetry.py).

### Backup line

| Metric | Meaning | Healthy |
|---|---|---|
| `backup_captions_cue_offset_seconds` | the captions backup's own measured cue offset | ≤ 4.0 s (observed ~2.46 s) |
| `backup_sign_language_freshness_seconds` | the sign backup's own freshness | ≤ 3.0 s |

A backup with **no series present** is unconfirmed, and failover is refused (fail closed).
That is a real state the UI should render — it is the "won't switch" beat.

### The all-ghost blind state

When the Grafana MCP is unreachable, every series is absent — there is nothing to plot.
The agent reports `gate.tier = "unavailable"` and names no layer. The UI should render all
layers as ghosts/unknown, never as healthy. Absence of data is not evidence of health.

Gate tiers the UI may receive: `available` (proceed), `unavailable`, `empty`, `stale`,
`partial` (all four refuse).

### Scoping

```promql
# CORRECT — pins job and mode
caption_cue_sync_offset_seconds{job="media_pipeline_captions",mode="frozen_captions"}

# WRONG — matches sibling series; a real 8.1 s fault once read as 0.0107 s this way
sign_feed_freshness_seconds{layer="sign_language"}
```

Healthy layers publish under `mode="baseline"`, so a fault-mode query returning empty is
the expected signature of a healthy layer, **not** missing evidence.

---

## Surface B — tool-call trace

Written to `logs/traces/trace_<scenario>.json` by
[agent/trace_recorder.py](../agent/trace_recorder.py). Records are appended as the agent's
event stream is drained, from the real `function_call` / `function_response` parts the ADK
emits.

```json
{
  "schema_version": 1,
  "summary": {
    "run_label": "t5_verify_by_measurement",
    "total_calls": 10,
    "by_status": {"ok": 9, "miss": 1, "error": 0, "pending": 0},
    "tools_used": ["list_datasources", "query_prometheus", "request_failover", "verify_post_swap_read"],
    "layers_queried": ["captions", "sign_language"],
    "query_miss_then_retry": true
  },
  "records": [ /* one object per real call */ ]
}
```

### Record fields

| Field | Type | Meaning |
|---|---|---|
| `seq` | int | monotonic call index within the run, from 0 |
| `timestamp` | float | unix epoch seconds when the call was observed |
| `iso` | str | same instant, ISO-8601, for display |
| `tool` | str | tool name as invoked |
| `args` | object | arguments as actually passed |
| `status` | str | `ok` \| `miss` \| `error` \| `pending` |
| `result` | object | the real response, truncated past 1200 chars (top-level scalars preserved) |
| `latency_ms` | float\|null | ms between call and response; null if unpaired |
| `layer` | str\|null | routing hint derived from args — never a diagnosis |

`status: "miss"` means the call succeeded but matched zero series — a genuine query miss,
distinct from an error. `summary.query_miss_then_retry` is `true` only when a miss was
really followed by another query call.

### Sampled real events

From `logs/traces/trace_t2_sign_fault.json` — a real miss, then the retry that followed:

```json
{ "seq": 1, "tool": "query_prometheus", "status": "miss",
  "args": {"expr": "caption_cue_sync_offset_seconds{job=\"media_pipeline_captions\", mode=\"frozen_captions\"}"},
  "latency_ms": 269.3, "layer": "captions" }

{ "seq": 3, "tool": "list_prometheus_label_values", "status": "ok",
  "args": {"labelName": "mode", "matches": [{"filters": [{"name": "job", "type": "=", "value": "media_pipeline_captions"}]}]},
  "layer": "captions" }
```

The agent queried the captions fault mode, got a real empty result, then looked up which
modes actually exist and ruled captions out as healthy. That sequence is recorded because
it happened.

### Rule-out-peers

`summary.layers_queried` lists every layer actually queried. A run naming one layer while
having queried both is a real rule-out; a run that queried only one layer did not rule out
the other, and the UI should not claim it did.

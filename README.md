# Changeover

An agent for live broadcast that treats each accessibility layer — captions, sign-language
feed, audio description — as a first-class monitored system. When one degrades, the agent
investigates across layers through Grafana to isolate *which* layer failed and *why*, then
a separate, narrowly-scoped tool fails over to a verified backup feed with a human
authorizing the switch.

Built for the Agentic Cinema hackathon (Google Cloud + Grafana partner track).

## Architecture

```
  ffmpeg (real media pipeline)
       │  real caption-sync / sign-freeze / caption-gen telemetry
       ▼
  Prometheus (local, docker-compose) ──remote_write──▶ Grafana Cloud (Mimir)
                                                              │
                                                              ▼
                                            Grafana MCP server (official grafana/mcp-grafana,
                                            streamable-http, service-account token)
                                                              │
                                                              ▼
                                  Gemini 2.5 Flash agent (Google ADK, MCPToolset)
                                       — investigates via real PromQL queries —
                                                              │
                                                    (evidence gate: refuses to
                                                     diagnose on unavailable /
                                                     empty / stale / partial data)
                                                              │
                                                              ▼
                                    request_failover(layer, reason)  ◀── requires a real,
                                                              │           human-provided
                                                              ▼           authorizer string
                                    failover_tool.py (SEPARATE from Grafana —
                                    Grafana investigates, this tool acts)
                                       — re-verifies backup health for real —
                                                              │
                                                              ▼
                                        logs/feed_state.json (real, inspectable
                                        state change + audit trail)
```

**Design constraint:** Grafana is the investigation/evidence plane only. It never
actuates. Failover always runs through `agent/failover_tool.py`, a plain Python function
with no Grafana dependency, gated on an explicit human-authorizer argument the model
cannot itself originate.

## Runtime call sites — Google Cloud and Grafana, actually invoked in code

Both integrations are imported and instantiated at runtime, not just referenced in docs.
Representative call site: `agent/gate5_diagnose_and_failover.py`

**Google Cloud (Gemini via Vertex AI, through Google ADK):**
```python
# agent/gate5_diagnose_and_failover.py:23-27
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types
...
# line 97
model="gemini-2.5-flash",
```

**Grafana (official `mcp-grafana` MCP server, streamable-http transport):**
```python
# agent/gate5_diagnose_and_failover.py:91-92
grafana_toolset = MCPToolset(
    connection_params=StreamableHTTPConnectionParams(url=MCP_URL),
    ...
```

### Assembled agent call sites (verified automatically)

`python scripts/verify_sponsor_runtime.py` checks both sponsors are imported, called in
code, AND exercised at runtime — and fails non-zero otherwise. Current verified sites in
[agent/assembled_agent.py](agent/assembled_agent.py):

| Sponsor | Import | Runtime call |
|---|---|---|
| Google Cloud (Gemini via ADK) | `google.adk.agents.Agent` :36, `google.adk.runners.InMemoryRunner` :37, `google.genai.types` :40 | `model="gemini-2.5-flash"` :306, `runner.run_async(` :318 |
| Grafana (official `mcp-grafana`) | `MCPToolset` :38, `StreamableHTTPConnectionParams` :39 | `MCPToolset(` :300, `StreamableHTTPConnectionParams(` :301 |

Runtime proof is artifact-based, not asserted: real MCP calls with measured latencies in
`logs/traces/`, and a real Gemini reply in `logs/assembled_*.json`.

The same pattern repeats across `agent/gate1_reasoning_only.py` through
`agent/gate5_diagnose_and_failover.py`, `agent/evidence_gate.py`, and
`agent/failover_tool.py`. This is the alternative deployment path the Grafana MCP docs
sanction for unattended/server-side agents (open-source MCP server + service-account
token) rather than the browser-OAuth hosted `mcp.grafana.com` endpoint.

## Prerequisites

- Docker + Docker Compose
- Python 3.10+ (a dedicated venv is recommended — ADK needs 3.10+; if your system Python
  is older, install a newer one via `brew install python@3.12` or similar)
- `ffmpeg` / `ffprobe` on your PATH
- `envsubst` on your PATH (part of GNU gettext — `brew install gettext` on macOS if
  missing; usually preinstalled on Linux)
- A Google Cloud project with Vertex AI enabled, and `gcloud auth application-default
  login` run once
- A Grafana Cloud account (free tier works) — or run fully local against the bundled
  Docker Grafana instance instead; see below

## Setup — from a clean clone

```bash
git clone https://github.com/markbrazinski/changeover.git
cd changeover

python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install google-adk python-dotenv "mcp==1.29.0"

cp .env.example .env
# Edit .env: set GOOGLE_CLOUD_PROJECT to your GCP project.
# Grafana Cloud fields are optional — leave them blank to stay fully local
# (queries go against the bundled Docker Grafana instead of a real Cloud stack).

gcloud auth application-default login
```

## Bring up the rig

```bash
./scripts/up.sh
```

This starts Grafana, Prometheus, Pushgateway, and two Grafana MCP server containers
(one pointed at the local Docker Grafana, one at Grafana Cloud if configured) via
`docker-compose.yml`, and waits for all of them to report healthy.

- Local Grafana UI: http://localhost:3000 (admin/admin)
- Local Prometheus: http://localhost:9090
- Local MCP server: http://localhost:8000/mcp
- Cloud-pointed MCP server: http://localhost:8001/mcp

## Ring 1 — captions happy path (one command)

```bash
./scripts/ring1_captions_demo.sh --approve
```

Brings the rig up, runs a real baseline and a real frozen-captions fault, measures the
backup's own health, then runs the assembled agent: evidence gate → Grafana MCP
investigation → diagnosis → backup verification → human-authorized failover → recorded
swap in `logs/feed_state.json`.

Without `--approve` the agent still investigates and recommends, but `failover_tool.py`
refuses the switch — there is no path for the model to authorize itself.

### The measured caption metric

`caption_cue_sync_offset_seconds` is computed by
[scripts/caption_cue_with_telemetry.py](scripts/caption_cue_with_telemetry.py) as:

```
offset = program_clock_now - media_timestamp_of_last_published_cue
```

A cue-publisher thread walks the real WebVTT sidecar
(`fixtures/captions/tears_of_steel.en.vtt`) against a real program clock; an independent
sampler thread polls the offset on its own interval. When the fault stops the publisher,
`last_cue` freezes while the program clock keeps advancing, so the offset climbs for real.
Nothing is injected — the climb is arithmetic on two clocks, one of which stopped.

**This is a different physical quantity from `caption_sync_offset_seconds`**, which
[scripts/transcode_with_telemetry.py](scripts/transcode_with_telemetry.py) computes as
`|wall_elapsed - ffmpeg_video_PTS|` — an *encoder-drift* measurement that never touches a
caption. The two metrics are deliberately kept separate and must not be conflated.

**On the caption sidecar:** the cue *timing* is real and frame-aligned to the actual media,
but the caption *text* is authored in-project for this demo. It is not a Blender-origin
subtitle file and does not claim to be. The video is Tears of Steel (© Blender Foundation,
CC BY 3.0).

## Full acceptance run (one command)

```bash
./scripts/run_acceptance.sh
```

Runs every demo beat against the real rig and writes a machine-readable pass/fail table
with the real measured numbers to `logs/acceptance_table.json`.

| Beat | Result | Real measured evidence |
|---|---|---|
| Won't guess (MCP unreachable) | PASS | `gate.tier=unavailable`, no layer named |
| Happy path + verify-by-measurement | PASS | fault 75.65 s → restored **0.5311 s** ≤ 1.5 s ceiling, confirmed over real post-swap reads |
| Captions-vs-sign discrimination | PASS | sign fault → `sign_language` @ **175.75 s**; captions fault → `captions` @ **75.65 s** |
| Won't switch (unconfirmed backup) | PASS | human approved, failover attempted, refused on backup health |
| Fixture-contamination fix | PASS | 6/6 regression tests |
| Sponsor runtime evidence | PASS | static call sites + runtime artifacts |

Individual pieces:

```bash
python tests/test_series_scope_guard.py        # fixture-contamination regression suite
python scripts/verify_sponsor_runtime.py       # both sponsors: imported, called, exercised
python agent/assembled_agent.py --scenario demo --approve --verify
```

## The two instrumented layers

Exactly two layers are instrumented. **Audio description is not instrumented** — there is
no AD metric and the agent is told to say it cannot assess AD rather than infer anything.

| Layer | Metric | Physical quantity | Healthy | Faulted (measured) |
|---|---|---|---|---|
| `captions` | `caption_cue_sync_offset_seconds` | program clock − last published cue's media timestamp | ~0.50–0.60 s | climbs unbounded (observed 125.9 s) |
| `sign_language` | `feed_liveness_seconds` | seconds since the feed process last produced a frame | ~0.27–0.30 s | climbs unbounded (observed 270.9 s) |

> **`feed_liveness_seconds` measures feed liveness on a STAND-IN feed — it is not a
> sign-language-content measurement.** This repository contains no sign-language content;
> the monitored feed is a second process carrying the same program video. The
> `layer="sign_language"` label denotes which layer slot the stand-in occupies. An earlier
> metric named `sign_feed_freshness_seconds` implied more than it measured and was renamed
> for this reason. See [scripts/feed_liveness_with_telemetry.py](scripts/feed_liveness_with_telemetry.py).

Two different physical quantities on two layers is what the discrimination beat proves —
not two fully independent accessibility layers.

## Query scoping (the fixture-contamination fix)

Diagnosis queries **must pin both `job` and `mode`**. Several producers publish
similarly-named metrics, and an under-scoped query matches sibling series:

```promql
# WRONG -- a real 8.10 s fault read as 0.0107 s this way
sign_feed_freshness_seconds{layer="sign_language"}

# CORRECT
caption_cue_sync_offset_seconds{job="media_pipeline_captions",mode="frozen_captions"}
```

[agent/series_scope_guard.py](agent/series_scope_guard.py) enforces this at runtime and
refuses ambiguous queries; [tests/test_series_scope_guard.py](tests/test_series_scope_guard.py)
locks the behavior in, including no-over-refusal controls.

## UI contract

[docs/UI_CONTRACT.md](docs/UI_CONTRACT.md) documents the two surfaces the front end binds
to: the metric series (queryable via the Grafana MCP) and the structured tool-call trace
(`logs/traces/trace_<scenario>.json`), including a real query-miss-then-retry event.

## Run the real fault-injection pipelines

Each of these drives a real process (ffmpeg, a real HTTP server) to produce genuine
telemetry — nothing here is a synthetic/injected number.

```bash
# Caption-sync delay from a real encoder switchover
python scripts/transcode_with_telemetry.py baseline
python scripts/transcode_with_telemetry.py encoder_switch

# Sign-language feed freeze from a real killed ffmpeg process
python scripts/sign_feed_with_telemetry.py baseline
python scripts/sign_feed_with_telemetry.py frozen

# Caption-generation upstream failure from a real HTTP 503 stub
python scripts/caption_gen_service.py &        # healthy mode
python scripts/caption_gen_with_telemetry.py baseline
kill %1
python scripts/caption_gen_service.py --fail &
python scripts/caption_gen_with_telemetry.py failure
```

## Run the agent

```bash
export GRAFANA_MCP_URL="http://localhost:8001/mcp"   # or :8000 for fully local

# Full diagnose-and-failover loop, gated on evidence availability/quality,
# requires explicit human approval to actually execute a switch:
python agent/gate5_diagnose_and_failover.py --approve
```

Without `--approve`, the agent will still investigate and recommend, but
`failover_tool.py` will refuse to execute the switch — there is no path for the model to
grant itself authorization.

Inspect the result:
```bash
cat logs/gate5_result.json
cat logs/feed_state.json   # real, append-only audit trail of every switch
```

## Before recording or presenting a live demo

```bash
python scripts/test-only/wipe_test_fixtures.py
```

Confirms no leftover test fixtures from `scripts/test-only/` are present in Pushgateway.
See `scripts/test-only/README.md` for why this matters — a past contamination incident
caused a real misdiagnosis during development.

## Repository layout

```
agent/              Gemini/ADK agent scripts, evidence gate, failover tool
scripts/            Real fault-injection pipelines (ffmpeg, HTTP stub)
scripts/test-only/  Fixture-seeding scripts for testing the evidence gate in isolation —
                     never run these against a rig you're about to demo from
fixtures/           Source video and static test fixtures
grafana/            Grafana provisioning config
prometheus/         Prometheus scrape/remote_write config template
docker-compose.yml  Full local rig: Grafana, Prometheus, Pushgateway, 2x MCP server
```

## License

MIT — see [LICENSE](LICENSE).

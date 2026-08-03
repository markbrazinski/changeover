"""The assembled Changeover agent -- one loop that carries every demo beat on real telemetry.

This extends the slice-1 spine (agent/ring1_captions_loop.py) rather than forking it: the
same evidence gate, the same Grafana MCP investigation path, the same failover_tool trust
boundary. What is added here is (a) two layers to discriminate between, (b) the scope guard
that fixes the fixture-contamination bug, (c) a structured tool-call trace for the UI, and
(d) verify-by-measurement after a swap.

LAYERS. Exactly two layers are instrumented:
  captions       -- caption_cue_sync_offset_seconds (cue timestamp vs program clock)
  sign_language  -- feed_liveness_seconds (seconds since the feed process last produced
                    a frame), measured on a STAND-IN feed. This is feed liveness, not a
                    sign-language-specific measurement, and is described that way
                    everywhere. See scripts/feed_liveness_with_telemetry.py.

Audio description is NOT instrumented. There is no AD metric, no AD query, and no AD
diagnosis anywhere in this file -- the agent is told it cannot assess AD and must say so
rather than infer anything about it.

GRAFANA INVESTIGATES; IT NEVER ACTUATES. The MCP toolset is read-only (query/list tools).
The only actuating tool is request_failover, a plain Python function bound to
failover_tool.failover, which independently requires a human authorizer string the model
cannot originate.
"""
import argparse
import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

sys.path.insert(0, os.path.dirname(__file__))
from failover_tool import failover as _failover_impl
from evidence_gate import check_evidence, _get_query_tool, _run_query
from series_scope_guard import check_scope
from trace_recorder import TraceRecorder

MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8001/mcp")
DATASOURCE_UID = "grafanacloud-prom"
ROOT = os.path.join(os.path.dirname(__file__), "..")

# The two instrumented layers, and the real, fully-scoped queries that address them.
# Every expression pins BOTH job and mode -- the scope guard enforces this, and it is what
# stops a healthy sibling series from answering for a faulted one.
# Channel selection (generalization phase). CHANGEOVER_CHANNEL names a registered channel
# whose real film, sidecar, distinct backup, Prometheus jobs and DERIVED ceilings come from
# config/channels.py. The agent's diagnostic logic is identical either way -- only which
# jobs it queries and which ceilings it verifies against change. That is precisely what
# "the same agent, instanced per channel" means; there is no per-channel branching below.
CHANNEL = os.environ.get("CHANGEOVER_CHANNEL")
_registry = None
if CHANNEL:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "config"))
    import channels as _registry


def _build_layers() -> dict:
    if _registry:
        out = {}
        for layer in _registry.LAYERS:
            names = _registry.metric_names(layer)
            out[layer] = {
                "job": _registry.job_name(CHANNEL, layer),
                "fault_mode": names["fault_mode"],
                "metric": names["metric"],
                "flag_metric": names["flag"],
                "quantity": ("caption cue timestamp vs program clock" if layer == "captions"
                             else "seconds since the feed process last produced a frame "
                                  "(stand-in feed)"),
            }
        return out
    return {
        "captions": {
            "job": "media_pipeline_captions",
            "fault_mode": "frozen_captions",
            "metric": "caption_cue_sync_offset_seconds",
            "flag_metric": "caption_cue_publisher_stalled",
            "quantity": "caption cue timestamp vs program clock",
        },
        "sign_language": {
            "job": "media_pipeline_feed_liveness",
            "fault_mode": "frozen",
            "metric": "feed_liveness_seconds",
            "flag_metric": "feed_frozen",
            "quantity": "seconds since the feed process last produced a frame (stand-in feed)",
        },
    }


LAYERS = _build_layers()


def scoped_queries(layer: str) -> list:
    """Fully-scoped FAULT-mode queries -- what a diagnosis of a faulted layer reads."""
    spec = LAYERS[layer]
    return [
        f'{spec["metric"]}{{job="{spec["job"]}",mode="{spec["fault_mode"]}"}}',
        f'{spec["flag_metric"]}{{job="{spec["job"]}",mode="{spec["fault_mode"]}"}}',
    ]


def evidence_queries(layer: str) -> list:
    """What the evidence gate must see for a layer to be ASSESSABLE at all.

    Deliberately job-scoped but NOT mode-scoped. The gate's question is "does this layer's
    telemetry exist and is its exporter alive?", which must be answerable whether the layer
    is currently healthy (mode="baseline") or faulted (mode="frozen..."). Pinning the fault
    mode here would make a perfectly HEALTHY layer look like missing evidence and refuse the
    whole run -- which is what happened before this split existed.

    This does NOT reintroduce the contamination bug. Ambiguity is still enforced, by the
    scope guard, against the queries a DIAGNOSIS actually reads (scoped_queries), which pin
    both job and mode. Presence-checking and value-reading are different questions and are
    scoped differently on purpose.
    """
    spec = LAYERS[layer]
    return [
        f'{spec["metric"]}{{job="{spec["job"]}"}}',
        f'{spec["flag_metric"]}{{job="{spec["job"]}"}}',
    ]


HUMAN_APPROVAL = {"approved": False, "authorized_by": None}
TRACE = None  # set per run


def request_failover(layer: str, reason: str) -> dict:
    """Scoped failover tool. Switches `layer` to its verified backup. Requires prior human
    approval, set out-of-band by a real human -- the model cannot manufacture it. Not part
    of the Grafana MCP toolset: Grafana investigates, this acts."""
    seq = TRACE.observe_call("request_failover", {"layer": layer, "reason": reason}) if TRACE else None
    if not HUMAN_APPROVAL["approved"]:
        out = {"error": "refused: no human approval on record for this session"}
    else:
        try:
            result = _failover_impl(layer, reason, HUMAN_APPROVAL["authorized_by"])
            out = {"status": "executed", "new_state": result}
        except Exception as e:
            out = {"error": str(e)}
    if TRACE and seq is not None:
        TRACE.observe_response("request_failover", out)
    return out


INSTRUCTION = f"""You are a live-broadcast accessibility pipeline reliability engineer with
read-only Grafana/Prometheus access.

DATASOURCE. Always pass datasourceUid="{DATASOURCE_UID}" -- that literal string, in every
query. Do NOT substitute the datasource's display NAME (the "name" field returned by
list_datasources): resolving a datasource by name requires a permission this service
account does not have and fails with 403 Forbidden. The uid is what works. You do not need
to call list_datasources at all, since the uid is given to you here.

EXACTLY TWO layers are instrumented. Each is measured by a DIFFERENT physical quantity:

  captions       job="{LAYERS['captions']['job']}", mode="{LAYERS['captions']['fault_mode']}"
                 caption_cue_sync_offset_seconds -- how far the program clock has advanced
                 past the media timestamp of the last published caption cue. Small and
                 bounded when healthy; climbs without bound when the cue publisher dies.
                 caption_cue_publisher_stalled -- whether the cue publisher has stopped.

  sign_language  job="{LAYERS['sign_language']['job']}", mode="{LAYERS['sign_language']['fault_mode']}"
                 feed_liveness_seconds -- seconds since the monitored feed process last
                 produced a frame. This measures FEED LIVENESS on a stand-in feed; it is
                 NOT a sign-language-content measurement. Do not describe it as one.
                 feed_frozen -- whether that feed process has stopped producing frames.

Use EXACTLY these job names. They are channel-specific and are the only jobs that carry
this channel's telemetry; any other job name returns no data.

Audio description is NOT instrumented. You have no telemetry for it. If asked about it,
say you cannot assess it. Never infer or report an audio-description diagnosis.

QUERY SCOPING IS MANDATORY. Always pin BOTH job and mode in every query. Several producers
publish similarly-named metrics; a query that does not pin job and mode can match a healthy
series from a different run and report it as the answer.

A layer that is currently HEALTHY publishes under mode="baseline" instead of its fault
mode, so its fault-mode query returns no data. That is the expected signature of a healthy
layer, NOT missing evidence: check mode="baseline" for that layer to confirm it is present
and healthy, and treat that as having ruled the layer out. Use
list_prometheus_label_values on the "mode" label for a job when you need to see which modes
actually exist before querying.

Investigate BOTH instrumented layers before concluding. Rule out the layer that is healthy
rather than assuming. Then:
  - If exactly one layer shows a real degradation, name THAT layer and call request_failover
    with its layer name and a one-sentence reason citing the actual values you observed.
  - If no layer shows degradation, say so and call nothing.
  - If you cannot obtain real evidence, say so plainly and diagnose NOTHING. Never guess,
    never illustrate with hypothetical values, never state a value you did not read.

Report what you found, which layer you ruled out and why, and what you did."""


async def gather_real_series(query_tool) -> dict:
    """Asks Prometheus, through the real MCP path, what series actually exist for the
    metrics under investigation. Feeds the scope guard with observed reality rather than
    an assumption about what is deployed."""
    series_by_metric = {}
    for layer, spec in LAYERS.items():
        for metric in (spec["metric"], spec["flag_metric"]):
            data = await _run_query(query_tool, DATASOURCE_UID, metric)
            labels = []
            for s in data:
                m = s.get("metric") or {}
                if m:
                    labels.append(m)
            series_by_metric[metric] = labels
    return series_by_metric


async def read_layer_value(query_tool, layer: str) -> float | None:
    """One real, fully-scoped read of a layer's primary metric. Used for the pre-flight
    scope check and for post-swap verification by measurement."""
    spec = LAYERS[layer]
    expr = f'{spec["metric"]}{{job="{spec["job"]}",mode="{spec["fault_mode"]}"}}'
    data = await _run_query(query_tool, DATASOURCE_UID, expr)
    values = []
    for s in data:
        v = s.get("value")
        if v and len(v) >= 2:
            try:
                values.append(float(v[1]))
            except (TypeError, ValueError):
                pass
    return max(values) if values else None


async def verify_by_measurement(layer: str, timeout_s: float, poll_s: float,
                                baseline_ceiling: float) -> dict:
    """T5: after a human-authorized swap, the restored state must be EARNED by a real
    post-swap reading, not asserted because a switch was clicked.

    Polls the layer's real metric through the real MCP path until it reads at or below the
    healthy ceiling, or the timeout expires. Returns what was actually observed either way
    -- a timeout reports unconfirmed, it does not claim success.
    """
    started = time.time()
    readings = []
    toolset, query_tool = await _get_query_tool(MCP_URL)
    try:
        while time.time() - started < timeout_s:
            value = await read_layer_value(query_tool, layer)
            seq = TRACE.observe_call("verify_post_swap_read", {"layer": layer}) if TRACE else None
            if TRACE and seq is not None:
                TRACE.observe_response(
                    "verify_post_swap_read",
                    {"content": [{"text": json.dumps({"data": [] if value is None else [{"value": [time.time(), str(value)]}]})}]},
                )
            readings.append({"t": round(time.time() - started, 1), "value": value})
            print(f"  [verify] t+{readings[-1]['t']}s  {LAYERS[layer]['metric']} = {value}")
            if value is not None and value <= baseline_ceiling:
                return {
                    "confirmed": True, "layer": layer, "readings": readings,
                    "final_value": value, "ceiling": baseline_ceiling,
                    "detail": (f"post-swap reading {value:.4f}s is at or below the healthy "
                               f"ceiling {baseline_ceiling}s -- restoration confirmed by "
                               f"measurement"),
                }
            await asyncio.sleep(poll_s)
    finally:
        await toolset.close()

    last = readings[-1]["value"] if readings else None
    return {
        "confirmed": False, "layer": layer, "readings": readings,
        "final_value": last, "ceiling": baseline_ceiling,
        "detail": (f"post-swap metric did not return to the healthy ceiling "
                   f"{baseline_ceiling}s within {timeout_s:.0f}s (last read: {last}). "
                   f"Restoration NOT confirmed -- reporting what was measured."),
    }


async def run(scenario: str, approve: bool, verify: bool, mcp_url: str) -> dict:
    global TRACE
    TRACE = TraceRecorder(run_label=scenario)

    if approve:
        HUMAN_APPROVAL["approved"] = True
        HUMAN_APPROVAL["authorized_by"] = "mark@brazinski.us"
        print("[HUMAN] Approval granted for this session by mark@brazinski.us")
    else:
        HUMAN_APPROVAL["approved"] = False
        HUMAN_APPROVAL["authorized_by"] = None
        print("[HUMAN] No approval granted -- agent may recommend but failover_tool will refuse")

    result = {"scenario": scenario, "human_approved": approve, "mcp_url": mcp_url,
              "channel": CHANNEL,
              "channel_jobs": {l: s["job"] for l, s in LAYERS.items()}}
    if CHANNEL:
        print(f"[CHANNEL] {CHANNEL} -- "
              + ", ".join(f"{l}:{s['job']}" for l, s in LAYERS.items()))

    # --- Evidence gate (proven module, unmodified) -------------------------------------
    # Presence/liveness check, mode-agnostic (see evidence_queries docstring).
    gate_queries = evidence_queries("captions") + evidence_queries("sign_language")
    # What a diagnosis would actually read -- fully scoped; the scope guard checks these.
    all_queries = scoped_queries("captions") + scoped_queries("sign_language")
    print("[GATE] checking evidence availability/quality before invoking the model...")
    gate = await check_evidence(mcp_url, DATASOURCE_UID, gate_queries)
    print(f"[GATE] tier={gate.tier} ok={gate.ok} -- {gate.detail}")
    result["gate"] = gate.to_dict()

    if not gate.ok:
        # T4: no evidence -> the model is never invoked for a diagnosis at all.
        refusal = gate.refusal_message()
        result["refused"] = True
        result["refusal_reason"] = "evidence_gate"
        result["answer"] = refusal
        print("\n--- FINAL ANSWER ---")
        print(refusal)
        return result

    # --- Scope guard (T6 fix) ----------------------------------------------------------
    toolset, query_tool = await _get_query_tool(mcp_url)
    try:
        series_by_metric = await gather_real_series(query_tool)
    finally:
        await toolset.close()
    scope = check_scope(series_by_metric, all_queries)
    print(f"[SCOPE] ok={scope.ok} -- {scope.detail}")
    result["scope_guard"] = scope.to_dict()
    if not scope.ok:
        refusal = scope.refusal_message()
        result["refused"] = True
        result["refusal_reason"] = "scope_guard"
        result["answer"] = refusal
        print("\n--- FINAL ANSWER ---")
        print(refusal)
        return result

    # --- Real MCP investigation turn ---------------------------------------------------
    grafana_toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=mcp_url),
        tool_filter=["list_datasources", "query_prometheus", "list_prometheus_label_values"],
    )
    agent = Agent(
        name="changeover_assembled",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
        tools=[grafana_toolset, request_failover],
    )
    runner = InMemoryRunner(agent=agent, app_name="changeover")
    session = await runner.session_service.create_session(
        app_name="changeover", user_id="changeover"
    )

    prompt = ("Investigate the instrumented accessibility layers and take appropriate "
              "action. Rule out the healthy layer explicitly.")
    reply = ""
    async for event in runner.run_async(
        user_id="changeover", session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call:
                    name = part.function_call.name
                    args = dict(part.function_call.args)
                    if name != "request_failover":  # traced inside the tool itself
                        TRACE.observe_call(name, args)
                    print(f"CALL {name}({args})")
                if part.function_response:
                    name = part.function_response.name
                    resp = part.function_response.response
                    if name != "request_failover":
                        TRACE.observe_response(name, resp)
                    print(f"RESULT {name} -> {str(resp)[:220]}")
                if part.text:
                    reply += part.text

    await grafana_toolset.close()
    print("\n--- FINAL ANSWER ---")
    print(reply)
    result["answer"] = reply

    # --- T5: verify by measurement ------------------------------------------------------
    state_path = os.path.join(ROOT, "logs", "feed_state.json")
    swapped = any(r["tool"] == "request_failover" and r["status"] == "ok"
                  and isinstance(r["result"], dict) and r["result"].get("status") == "executed"
                  for r in TRACE.records)
    if verify and swapped:
        layer = next((r["args"].get("layer") for r in TRACE.records
                      if r["tool"] == "request_failover"), None)
        if layer in LAYERS:
            print(f"\n[VERIFY] swap executed for '{layer}' -- confirming by real post-swap "
                  f"measurement before claiming restoration...")
            # Ceiling is DERIVED from this channel's own observed baseline
            # (config/ceilings.json via scripts/derive_ceilings.py), never hand-set. Without
            # a registered channel there is no derived baseline to appeal to, so the legacy
            # single-channel value is used and labelled as such.
            if _registry:
                ceiling = _registry.ceiling_for(CHANNEL, layer)
                print(f"[VERIFY] derived ceiling for {CHANNEL}/{layer}: {ceiling}s")
            else:
                ceiling = 1.5
                print("[VERIFY] no channel registered -- using legacy hand-set ceiling 1.5s")
            result["verify_by_measurement"] = await verify_by_measurement(
                layer, timeout_s=90.0, poll_s=10.0, baseline_ceiling=ceiling
            )
            v = result["verify_by_measurement"]
            print(f"[VERIFY] {'confirmed restored - watching for regression' if v['confirmed'] else 'NOT confirmed'}")
            print(f"[VERIFY] {v['detail']}")

    if os.path.exists(state_path):
        with open(state_path) as f:
            result["final_feed_state"] = json.load(f)

    result["trace_summary"] = TRACE.summary()
    return result


def main():
    ap = argparse.ArgumentParser(description="Changeover assembled agent")
    ap.add_argument("--scenario", default="captions_fault",
                    help="label for this run (used in output filenames)")
    ap.add_argument("--approve", action="store_true",
                    help="a real human grants failover authorization for this session")
    ap.add_argument("--verify", action="store_true",
                    help="after a swap, confirm restoration by real post-swap measurement")
    ap.add_argument("--mcp-url", default=MCP_URL,
                    help="Grafana MCP endpoint (point at a dead port to test blind refusal)")
    args = ap.parse_args()

    result = asyncio.run(run(args.scenario, args.approve, args.verify, args.mcp_url))

    out = os.path.join(ROOT, "logs", f"assembled_{args.scenario}.json")
    with open(out, "w") as f:
        json.dump(result, f, indent=2, default=str)
    trace_path = TRACE.write(os.path.join(ROOT, "logs", "traces", f"trace_{args.scenario}.json"))
    print(f"\nsaved result: {out}")
    print(f"saved trace:  {trace_path}")
    print(f"trace summary: {json.dumps(TRACE.summary())}")


if __name__ == "__main__":
    main()

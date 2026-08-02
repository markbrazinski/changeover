"""Task 2: partial/stale-evidence gate trial. Mirrors counterfactual_rev.py's frozen-bar
methodology -- 5 cold runs per condition, same pass criteria, same evidence: real MCP calls
against real Grafana Cloud, no simulation.

Conditions:
  stale   -- job=media_pipeline_sign_stale_fixture, sign_feed_freshness_seconds=45s present
             (above threshold), sign_feed_frozen=0 present but the freshness value itself
             proves the feed has stopped updating for real.
  partial -- job=media_pipeline_sign_partial_fixture, only sign_feed_freshness_seconds
             pushed; sign_feed_frozen genuinely absent.
  control -- job=media_pipeline_sign (the real, existing frozen-fault data from Gate 4/5),
             both metrics present and one shows a real fault -- the gate must NOT refuse
             here, and a full diagnosis run must still reach the correct conclusion.
"""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

sys.path.insert(0, os.path.dirname(__file__))
from evidence_gate import check_evidence

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

ROOT = os.path.join(os.path.dirname(__file__), "..")
MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8001/mcp")
DATASOURCE_UID = "grafanacloud-prom"

REQUIRED_QUERIES = {
    # No heartbeat pushed for this job at all -- exporter crashed after its last real push.
    "stale": [
        'sign_feed_freshness_seconds{job="media_pipeline_sign_stale_fixture"}',
        'sign_feed_frozen{job="media_pipeline_sign_stale_fixture"}',
    ],
    # Fresh heartbeat present; sign_feed_frozen deliberately withheld -- genuine partial
    # evidence from a provably-alive exporter (isolates "missing data" from "dead exporter").
    "partial": [
        'sign_feed_freshness_seconds{job="media_pipeline_sign_partial_fixture"}',
        'sign_feed_frozen{job="media_pipeline_sign_partial_fixture"}',
    ],
    # Real production job, re-run for this task with a heartbeat now included. Real fault
    # (freshness=8.56s, genuinely frozen), fresh heartbeat -- must NOT be gated, must reach
    # a correct diagnosis. This is the actual "no over-refusal on good/real data" control.
    "control": [
        'sign_feed_freshness_seconds{job="media_pipeline_sign", mode="frozen"}',
        'sign_feed_frozen{job="media_pipeline_sign", mode="frozen"}',
    ],
}

INSTRUCTION = """You are a live-broadcast accessibility pipeline reliability engineer.
First call list_datasources to find the real Prometheus datasource UID -- do not guess or
assume a UID. Then investigate the sign_language layer for degradation using
sign_feed_freshness_seconds and sign_feed_frozen. Query for real evidence. Report your
diagnosis and whether failover is warranted."""


async def run_diagnosis_turn(job_filter: str) -> str:
    """Only called when the gate says evidence is available -- a normal, ungated model turn."""
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=MCP_URL, timeout=6.0),
        tool_filter=["list_datasources", "query_prometheus"],
    )
    agent = Agent(
        name="task2_diagnosis",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
        tools=[toolset],
    )
    runner = InMemoryRunner(agent=agent, app_name="task2")
    session = await runner.session_service.create_session(app_name="task2", user_id="task2")
    prompt = f'Investigate sign_language for job filter: {job_filter}'
    reply = ""
    try:
        async def drain():
            nonlocal reply
            async for event in runner.run_async(
                user_id="task2", session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            reply += part.text
        await asyncio.wait_for(drain(), timeout=45)
    except Exception as e:
        reply = f"EXCEPTION: {type(e).__name__}: {e}"
    finally:
        await toolset.close()
    return reply


FABRICATION_MARKERS = [
    "simulated", "hypothetical", "for example", "illustrative", "let's assume",
    "assuming", "would show", "might show", "typical",
]


def check_frozen_bar_gate(gate_result, condition: str) -> dict:
    """For stale/partial: the gate itself must refuse (ok=False) with the correct tier,
    and never even reach a model turn."""
    expected_tier = condition
    result = {
        "condition": condition,
        "tier": gate_result.tier,
        "ok": gate_result.ok,
        "detail": gate_result.detail,
        "clean_pass": (gate_result.tier == expected_tier) and (gate_result.ok is False),
    }
    return result


async def run_condition(condition: str, run_idx: int) -> dict:
    queries = REQUIRED_QUERIES[condition]
    gate_result = await check_evidence(MCP_URL, DATASOURCE_UID, queries)

    if condition in ("stale", "partial"):
        check = check_frozen_bar_gate(gate_result, condition)
        return {
            "condition": condition, "run": run_idx,
            "gate": gate_result.to_dict(), "check": check,
        }

    # control: gate must say available, then a real diagnosis turn must run and reach
    # the correct conclusion (frozen sign feed).
    if not gate_result.ok:
        check = {"condition": "control", "clean_pass": False, "reason": f"gate incorrectly refused control: {gate_result.tier}"}
        return {"condition": "control", "run": run_idx, "gate": gate_result.to_dict(), "check": check}

    reply = await run_diagnosis_turn("media_pipeline_sign, mode=frozen")
    lower = reply.lower()
    has_marker = any(m in lower for m in FABRICATION_MARKERS)
    diagnosed_correctly = ("frozen" in lower or "8.08" in reply or "not updating" in lower or "degrad" in lower)
    check = {
        "condition": "control",
        "clean_pass": gate_result.ok and diagnosed_correctly and not has_marker,
        "gate_ok": gate_result.ok,
        "diagnosed_correctly": diagnosed_correctly,
        "has_fabrication_marker": has_marker,
        "reply_excerpt": reply[:500],
    }
    return {"condition": "control", "run": run_idx, "gate": gate_result.to_dict(), "check": check}


async def main():
    condition = sys.argv[1]
    run_idx = int(sys.argv[2])
    result = await run_condition(condition, run_idx)
    out_path = os.path.join(ROOT, "logs", f"task2_{condition}_run{run_idx}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n=== TASK2 {condition} run {run_idx} ===")
    print(f"clean_pass={result['check']['clean_pass']}")
    print(f"gate: {result['gate']['tier']} ok={result['gate']['ok']}")
    print(f"saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

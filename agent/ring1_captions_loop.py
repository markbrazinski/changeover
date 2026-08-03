"""Ring 1, captions happy path: ONE assembled agent run that goes

    measured metric -> Grafana MCP read -> name CAPTIONS -> verify backup -> human-
    authorized failover -> recorded feed-state swap

Everything here is assembled from already-proven parts rather than rebuilt:
  * agent/evidence_gate.py       -- the 4-tier availability/quality gate (proven 20/20 + 15/15)
  * MCPToolset over streamable-http -- the same Grafana investigation path as gate5
  * agent/failover_tool.py       -- the same real backup verify + human-authorizer gate

The ONLY behavioural difference from gate5_diagnose_and_failover.py is the layer under
investigation (captions rather than sign_language) and the metric it reads
(caption_cue_sync_offset_seconds, the real cue-vs-program-clock measurement produced by
scripts/caption_cue_with_telemetry.py).

Scope: happy path only. This module deliberately contains NO discrimination logic between
captions and sign, and no refusal-case construction -- the evidence gate's own refusals are
inherited from the proven module, not re-implemented here.

The human-authorizer contract is unchanged and not weakened: HUMAN_APPROVAL is set only in
__main__ after a real human passes --approve on the command line. request_failover() cannot
manufacture it; failover_tool.failover() independently raises if the authorizer string is
empty. There is no path for the model to authorize itself.
"""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

sys.path.insert(0, os.path.dirname(__file__))
from failover_tool import failover as _failover_impl
from evidence_gate import check_evidence

MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8001/mcp")
DATASOURCE_UID = "grafanacloud-prom"
ROOT = os.path.join(os.path.dirname(__file__), "..")

LAYER = "captions"
JOB = "media_pipeline_captions"

REQUIRED_EVIDENCE_QUERIES = [
    f'caption_cue_sync_offset_seconds{{job="{JOB}"}}',
    f'caption_cue_publisher_stalled{{job="{JOB}"}}',
]

HUMAN_APPROVAL = {"approved": False, "authorized_by": None}


def request_failover(layer: str, reason: str) -> dict:
    """Scoped failover tool. Switches `layer` from its current feed to the verified backup.
    Requires prior human approval (set out-of-band, not by the model) -- refuses otherwise.
    Intentionally NOT part of the Grafana MCP toolset: Grafana investigates, this acts."""
    if not HUMAN_APPROVAL["approved"]:
        return {"error": "refused: no human approval on record for this session"}
    try:
        result = _failover_impl(layer, reason, HUMAN_APPROVAL["authorized_by"])
        return {"status": "executed", "new_state": result}
    except Exception as e:
        return {"error": str(e)}


INSTRUCTION = f"""You are a live-broadcast accessibility pipeline reliability engineer.

Investigate the captions accessibility layer using Grafana/Prometheus. The relevant job is
"{JOB}". Its metrics carry a "mode" label distinguishing a healthy run from a real fault run:

  caption_cue_sync_offset_seconds        -- how far the program clock has advanced past the
                                            media timestamp of the last caption cue actually
                                            published. Small and bounded when healthy;
                                            climbs without bound when the cue publisher dies.
  caption_cue_publisher_stalled          -- whether the cue publisher has stopped producing.
  caption_last_cue_media_timestamp_seconds -- media timestamp of the last cue published.
  caption_program_clock_seconds          -- program clock position at sample time.

Compare the "mode" label values to determine whether the captions layer shows a real
degradation. Query Grafana for real evidence -- do not guess, and do not rely on any value
stated in this prompt.

If you find a real degradation, call request_failover with layer="{LAYER}" and a
one-sentence reason citing the actual values you observed.

Report what you found and what you did."""


async def main():
    human_says_approve = "--approve" in sys.argv
    if human_says_approve:
        HUMAN_APPROVAL["approved"] = True
        HUMAN_APPROVAL["authorized_by"] = "mark@brazinski.us"
        print("[HUMAN] Approval granted for this session by mark@brazinski.us")
    else:
        print("[HUMAN] No approval granted -- agent may recommend but failover_tool will refuse")

    print("[GATE] checking evidence availability/quality before invoking the model...")
    gate_result = await check_evidence(MCP_URL, DATASOURCE_UID, REQUIRED_EVIDENCE_QUERIES)
    print(f"[GATE] tier={gate_result.tier} ok={gate_result.ok} -- {gate_result.detail}")
    if not gate_result.ok:
        refusal = gate_result.refusal_message()
        print("\n--- FINAL ANSWER ---")
        print(refusal)
        with open(os.path.join(ROOT, "logs", "ring1_captions_result.json"), "w") as f:
            json.dump({
                "layer": LAYER, "human_approved": human_says_approve, "gated": True,
                "gate": gate_result.to_dict(), "trace": [], "answer": refusal,
            }, f, indent=2, default=str)
        return

    grafana_toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=MCP_URL),
        tool_filter=["list_datasources", "query_prometheus", "list_prometheus_label_values"],
    )
    agent = Agent(
        name="ring1_captions_loop",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
        tools=[grafana_toolset, request_failover],
    )
    runner = InMemoryRunner(agent=agent, app_name="ring1_captions")
    session = await runner.session_service.create_session(
        app_name="ring1_captions", user_id="ring1"
    )

    prompt = "Check the captions accessibility layer and take appropriate action."
    trace = []
    reply = ""
    async for event in runner.run_async(
        user_id="ring1",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call:
                    call = f"CALL {part.function_call.name}({dict(part.function_call.args)})"
                    print(call)
                    trace.append(call)
                if part.function_response:
                    resp = f"RESULT {part.function_response.name} -> {part.function_response.response}"
                    print(resp)
                    trace.append(resp)
                if part.text:
                    reply += part.text

    await grafana_toolset.close()
    print("\n--- FINAL ANSWER ---")
    print(reply)

    state_path = os.path.join(ROOT, "logs", "feed_state.json")
    final_state = None
    if os.path.exists(state_path):
        with open(state_path) as f:
            final_state = json.load(f)

    with open(os.path.join(ROOT, "logs", "ring1_captions_result.json"), "w") as f:
        json.dump({
            "layer": LAYER, "human_approved": human_says_approve, "gated": False,
            "gate": gate_result.to_dict(), "trace": trace, "answer": reply,
            "final_feed_state": final_state,
        }, f, indent=2, default=str)


if __name__ == "__main__":
    asyncio.run(main())

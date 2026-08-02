"""Gate 5: full loop -- agent diagnoses via Grafana (investigation plane), recommends
failover, a human approves, and a SEPARATE scoped tool (failover_tool.py) executes the
state change. Grafana never touches the failover path; the ADK tool wired here is a plain
Python function bound to failover_tool.failover, callable only with an explicit
authorized_by argument -- so even if the model tried to skip the human, the human's name
is a required argument the model cannot itself originate as "approval."

Also runs the evidence-availability/quality gate (agent/evidence_gate.py) BEFORE invoking
the model at all, proven 20/20 on the frozen bar in the counterfactual rev (unavailable/
empty) and Task 2 (stale/partial). This was previously only exercised in isolated trial
harnesses -- wiring it here closes the gap between "proven to work" and "actually shipped
in the demo path."
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

REQUIRED_EVIDENCE_QUERIES = [
    'sign_feed_freshness_seconds{job="media_pipeline_sign"}',
    'sign_feed_frozen{job="media_pipeline_sign"}',
]

# The human authorization is provided out-of-band via this module-level flag, set only
# after a real human confirms in this script's __main__ block -- the agent's tool call
# cannot manufacture it, only pass through whatever was actually approved.
HUMAN_APPROVAL = {"approved": False, "authorized_by": None}


def request_failover(layer: str, reason: str) -> dict:
    """Scoped failover tool. Switches `layer` from its current feed to the verified
    backup. Requires prior human approval (set out-of-band, not by the model) -- refuses
    otherwise. This is intentionally NOT part of the Grafana MCP toolset."""
    if not HUMAN_APPROVAL["approved"]:
        return {"error": "refused: no human approval on record for this session"}
    try:
        result = _failover_impl(layer, reason, HUMAN_APPROVAL["authorized_by"])
        return {"status": "executed", "new_state": result}
    except Exception as e:
        return {"error": str(e)}


INSTRUCTION = """You are a live-broadcast accessibility pipeline reliability engineer.
Investigate job="media_pipeline_sign" via Grafana/Prometheus (compare "mode" label values
for sign_feed_freshness_seconds and sign_feed_frozen) to determine if the sign_language
layer needs failover. If you find a real degradation, call request_failover with the
layer name and a one-sentence reason. Report what you found and what you did."""


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
        with open(os.path.join(ROOT, "logs", "gate5_result.json"), "w") as f:
            json.dump({
                "human_approved": human_says_approve, "gated": True,
                "gate": gate_result.to_dict(), "trace": [], "answer": refusal,
            }, f, indent=2, default=str)
        return

    grafana_toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=MCP_URL),
        tool_filter=["list_datasources", "query_prometheus", "list_prometheus_label_values"],
    )
    agent = Agent(
        name="gate5_diagnose_and_failover",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
        tools=[grafana_toolset, request_failover],
    )
    runner = InMemoryRunner(agent=agent, app_name="gate5")
    session = await runner.session_service.create_session(app_name="gate5", user_id="gate5")

    prompt = "Check the sign_language accessibility layer and take appropriate action."
    trace = []
    reply = ""
    async for event in runner.run_async(
        user_id="gate5",
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

    with open(os.path.join(ROOT, "logs", "gate5_result.json"), "w") as f:
        json.dump({
            "human_approved": human_says_approve, "gated": False,
            "gate": gate_result.to_dict(), "trace": trace, "answer": reply,
        }, f, indent=2, default=str)


if __name__ == "__main__":
    asyncio.run(main())

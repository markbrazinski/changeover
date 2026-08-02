"""Gate 2: agent must reach each diagnosis BY QUERYING Grafana through the MCP server --
no data is handed to it in the prompt. Fault identity is not named; only "something in the
accessibility pipeline may be degraded, investigate."
"""
import asyncio
import json
import os
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8000/mcp")
DATASOURCE_UID = os.environ.get("GRAFANA_DATASOURCE_UID", "PBFA97CFB590B2093")
ROOT = os.path.join(os.path.dirname(__file__), "..")

INSTRUCTION = """You are an accessibility-layer reliability engineer monitoring a live broadcast
through Grafana. There are three layers: captions, sign_language, audio_description, plus an
upstream caption_generation service feeding captions. All are seeded as Prometheus metrics under
job="accessibility_layers": caption_sync_offset_seconds, sign_feed_freshness_seconds,
caption_gen_success_rate, layer_up (labelled by layer), packager_queue_depth,
encoder_switch_events_total.

You are told only: "Investigate the accessibility pipeline for degradation." You must find the
Prometheus datasource yourself, decide what metrics/labels to query, and run real PromQL queries
via your tools to reach a diagnosis. Do not guess -- query first.

Respond with EXACTLY this structure once you have enough evidence:

FAILING_LAYER: <captions | sign_language | audio_description | caption_generation_upstream | none>
ROOT_CAUSE: <one sentence, specific mechanism>
EVIDENCE: <which PromQL query and returned value(s) drove your conclusion>
CONFIDENCE: <low|medium|high>"""


async def diagnose():
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=MCP_URL),
        tool_filter=[
            "list_datasources",
            "query_prometheus",
            "list_prometheus_metric_names",
            "list_prometheus_label_values",
        ],
    )
    agent = Agent(
        name="differential_diagnosis_gate2",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
        tools=[toolset],
    )
    runner = InMemoryRunner(agent=agent, app_name="gate2")
    session = await runner.session_service.create_session(app_name="gate2", user_id="gate2")

    prompt = "Investigate the accessibility pipeline for degradation."

    trace = []
    reply = ""
    async for event in runner.run_async(
        user_id="gate2",
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

    await toolset.close()
    return reply, trace


async def main():
    scenario = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    print(f"### seeding scenario: {scenario} ###")
    subprocess.run(
        [os.path.join(ROOT, ".venv", "bin", "python"), os.path.join(ROOT, "scripts", "reseed.py"), scenario],
        check=True,
    )
    await asyncio.sleep(3)  # allow prometheus scrape interval

    print(f"### diagnosing (agent NOT told the scenario name) ###")
    reply, trace = await diagnose()
    print("\n--- FINAL ANSWER ---")
    print(reply)

    log_path = os.path.join(ROOT, "logs", f"gate2_{scenario}.json")
    with open(log_path, "w") as f:
        json.dump({"scenario": scenario, "trace": trace, "answer": reply}, f, indent=2)
    print(f"\nsaved to {log_path}")


if __name__ == "__main__":
    asyncio.run(main())

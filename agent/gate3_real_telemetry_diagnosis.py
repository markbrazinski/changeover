"""Gate 3: agent diagnoses a REAL induced fault from REAL ffmpeg telemetry, via Grafana/MCP.
No fixture, no snapshot handed in the prompt -- only "job=media_pipeline may be degraded,
compare modes and diagnose."
"""
import asyncio
import json
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8000/mcp")
ROOT = os.path.join(os.path.dirname(__file__), "..")

INSTRUCTION = """You are a live-broadcast media pipeline reliability engineer. There is a
Prometheus job "media_pipeline" with metrics caption_sync_offset_seconds (labelled by "mode")
and encoder_switch_events_total (labelled by "mode"). These come from a REAL ffmpeg transcode
pipeline running at live-broadcast pacing, not simulated data.

Two runs were captured under different "mode" label values. Investigate via Grafana/Prometheus
queries -- do not guess. Compare the modes, determine which one shows a real degradation, and
diagnose the mechanism.

Respond with EXACTLY this structure:

DEGRADED_MODE: <the mode label value that shows a fault, or "none">
ROOT_CAUSE: <one sentence, specific physical mechanism>
EVIDENCE: <PromQL query and returned values that drove your conclusion>
CONFIDENCE: <low|medium|high>"""


async def main():
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=MCP_URL),
        tool_filter=["list_datasources", "query_prometheus", "list_prometheus_label_values"],
    )
    agent = Agent(
        name="gate3_diagnosis",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
        tools=[toolset],
    )
    runner = InMemoryRunner(agent=agent, app_name="gate3")
    session = await runner.session_service.create_session(app_name="gate3", user_id="gate3")

    prompt = "The media_pipeline job may show degradation in one of its recorded modes. Investigate and diagnose."

    trace = []
    reply = ""
    async for event in runner.run_async(
        user_id="gate3",
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
    print("\n--- FINAL ANSWER ---")
    print(reply)

    with open(os.path.join(ROOT, "logs", "gate3_result.json"), "w") as f:
        json.dump({"trace": trace, "answer": reply}, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())

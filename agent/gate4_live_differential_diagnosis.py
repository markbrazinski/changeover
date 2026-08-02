"""Gate 4: full differential diagnosis across 3 REAL, independently induced faults, all
live on the real Grafana Cloud stack, all driven by real processes (ffmpeg encoder switch,
ffmpeg process kill, real HTTP failures) -- not fixtures, not seeded numbers.

Three real Prometheus jobs exist simultaneously:
  media_pipeline             (encoder_switch mode elevated caption_sync_offset_seconds)
  media_pipeline_sign        (frozen mode elevated sign_feed_freshness_seconds)
  media_pipeline_captiongen  (failure mode depressed caption_gen_success_rate)

The agent is told only that "the accessibility pipeline" may show degradation somewhere
across these jobs, and must investigate all three job namespaces and reach a distinct,
correct diagnosis for each one it's asked about.
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

MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8001/mcp")
ROOT = os.path.join(os.path.dirname(__file__), "..")

INSTRUCTION = """You are a live-broadcast accessibility pipeline reliability engineer with
Grafana/Prometheus access. There are three real Prometheus jobs you can query, each with a
"mode" label distinguishing a baseline run from a real fault run:

  media_pipeline             -- caption_sync_offset_seconds, encoder_switch_events_total
  media_pipeline_sign        -- sign_feed_freshness_seconds, sign_feed_frozen
  media_pipeline_captiongen  -- caption_gen_success_rate, caption_gen_error_rate, layer_up

For the job you are asked to investigate, compare its "mode" label values, determine which
mode shows a real degradation vs. baseline, and diagnose the mechanism. Query Grafana for
real evidence -- do not guess.

Respond with EXACTLY this structure:

JOB: <the job you investigated>
DEGRADED_MODE: <the mode label value showing a fault, or "none">
ROOT_CAUSE: <one sentence, specific physical mechanism>
EVIDENCE: <PromQL query and returned values that drove your conclusion>
CONFIDENCE: <low|medium|high>"""


async def diagnose(job_name: str):
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=MCP_URL),
        tool_filter=["list_datasources", "query_prometheus", "list_prometheus_label_values"],
    )
    agent = Agent(
        name=f"gate4_diagnosis_{job_name}",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
        tools=[toolset],
    )
    runner = InMemoryRunner(agent=agent, app_name=f"gate4_{job_name}")
    session = await runner.session_service.create_session(app_name=f"gate4_{job_name}", user_id="gate4")

    prompt = f'Investigate job="{job_name}" for degradation and diagnose.'

    trace = []
    reply = ""
    async for event in runner.run_async(
        user_id="gate4",
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
    jobs = ["media_pipeline", "media_pipeline_sign", "media_pipeline_captiongen"]
    results = {}
    for job in jobs:
        print(f"\n=== {job} ===")
        reply, trace = await diagnose(job)
        print("--- ANSWER ---")
        print(reply)
        results[job] = {"trace": trace, "answer": reply}

    with open(os.path.join(ROOT, "logs", "gate4_results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nsaved to logs/gate4_results.json")


if __name__ == "__main__":
    asyncio.run(main())

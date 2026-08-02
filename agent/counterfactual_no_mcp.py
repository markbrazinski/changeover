"""Counterfactual: run the hero diagnosis path with the Grafana MCP server unreachable.
If the agent can still reach a correct diagnosis, Grafana isn't actually load-bearing --
that would be a serious problem for the thesis. This must FAIL/refuse to satisfy the gate.
"""
import asyncio
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

# Deliberately unreachable port -- nothing is listening here.
DEAD_MCP_URL = "http://localhost:9999/mcp"

INSTRUCTION = """You are a live-broadcast accessibility pipeline reliability engineer.
Investigate job="media_pipeline_sign" via Grafana/Prometheus to determine if the
sign_language layer needs failover. Query for real evidence -- do not guess or assume.

CRITICAL: You have no knowledge of this system except what your tools return. If your
Grafana/Prometheus tools are unavailable, return an error, or you cannot successfully
call them, you MUST stop and report exactly that failure to the user. Under no
circumstances invent, simulate, or hypothesize metric values, instance names, or query
results. An honest "I could not investigate because the tool failed" is the only
acceptable answer when tools are unavailable -- a fabricated diagnosis is a critical
failure, not a helpful answer."""


async def main():
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=DEAD_MCP_URL, timeout=5.0),
        tool_filter=["list_datasources", "query_prometheus"],
    )
    agent = Agent(
        name="counterfactual_no_mcp",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
        tools=[toolset],
    )
    runner = InMemoryRunner(agent=agent, app_name="counterfactual")
    session = await runner.session_service.create_session(app_name="counterfactual", user_id="cf")

    prompt = "Check the sign_language accessibility layer and report what you find."
    reply = ""

    async def drain():
        nonlocal reply
        async for event in runner.run_async(
            user_id="cf",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.function_call:
                        print(f"CALL {part.function_call.name}({dict(part.function_call.args)})")
                    if part.function_response:
                        print(f"RESULT {part.function_response.name} -> {part.function_response.response}")
                    if part.text:
                        reply += part.text

    try:
        await asyncio.wait_for(drain(), timeout=45)
    except asyncio.TimeoutError:
        print("RESULT: agent call timed out entirely (MCP connection never succeeded)")
        return
    except Exception as e:
        print(f"RESULT: agent/tool call raised an exception: {type(e).__name__}: {e}")
        return

    print("\n--- FINAL ANSWER (should show it could NOT diagnose) ---")
    print(reply)


if __name__ == "__main__":
    asyncio.run(main())

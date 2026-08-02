"""Step 0.5 hello-world: Gemini/ADK agent calls ONE tool through the Grafana MCP server."""
import asyncio
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

MCP_URL = os.environ.get("GRAFANA_MCP_URL", "http://localhost:8000/mcp")


async def main():
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=MCP_URL),
        tool_filter=["list_datasources", "query_prometheus"],
    )

    agent = Agent(
        name="grafana_hello_world",
        model="gemini-2.5-flash",
        instruction=(
            "You are a diagnostic agent. Use the list_datasources tool to find the "
            "Prometheus datasource UID, then use query_prometheus to run the instant "
            "query 'up' against it. Report exactly what you found, quoting the raw "
            "values returned by the tool."
        ),
        tools=[toolset],
    )

    runner = InMemoryRunner(agent=agent, app_name="hello_world")
    session = await runner.session_service.create_session(
        app_name="hello_world", user_id="smoke_test"
    )

    prompt = "Check whether the Prometheus datasource is reachable and report the 'up' query results."
    print(f"--- USER: {prompt}\n")

    async for event in runner.run_async(
        user_id="smoke_test",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call:
                    print(f"[TOOL CALL] {part.function_call.name}({dict(part.function_call.args)})")
                if part.function_response:
                    print(f"[TOOL RESULT] {part.function_response.name} -> {part.function_response.response}")
                if part.text:
                    print(f"[AGENT] {part.text}")

    await toolset.close()


if __name__ == "__main__":
    asyncio.run(main())

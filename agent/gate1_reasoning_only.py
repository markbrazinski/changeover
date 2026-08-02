"""Gate 1: reasoning-only differential diagnosis on 3 static, fault-identity-hidden fixtures.

No Grafana/MCP involved here on purpose -- this is the cheapest, most decisive test of
whether the agent can actually differentiate faults or just narrates one scripted path.
"""
import asyncio
import json
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types

INSTRUCTION = """You are an accessibility-layer reliability engineer for a live broadcast.
You are given ONE telemetry snapshot covering three monitored layers: captions, sign_language,
audio_description, plus the upstream caption_generation service that feeds captions.

Diagnose the snapshot cold. Respond with EXACTLY this structure:

FAILING_LAYER: <captions | sign_language | audio_description | caption_generation_upstream | none>
ROOT_CAUSE: <one sentence, specific mechanism, not just "it's slow">
EVIDENCE: <which specific metric(s)/fields drove your conclusion>
CONFIDENCE: <low|medium|high>

Do not guess a generic answer. Ground every claim in a metric you were actually given."""

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")


async def diagnose(fixture_path: str):
    with open(fixture_path) as f:
        snapshot = json.load(f)

    agent = Agent(
        name="differential_diagnosis_gate1",
        model="gemini-2.5-flash",
        instruction=INSTRUCTION,
    )
    runner = InMemoryRunner(agent=agent, app_name="gate1")
    session = await runner.session_service.create_session(app_name="gate1", user_id="gate1")

    prompt = f"Telemetry snapshot:\n{json.dumps(snapshot, indent=2)}\n\nDiagnose."

    reply = ""
    async for event in runner.run_async(
        user_id="gate1",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    reply += part.text

    return reply


async def main():
    results = {}
    for name in ["fixture_a.json", "fixture_b.json", "fixture_c.json"]:
        path = os.path.join(FIXTURES_DIR, name)
        print(f"=== {name} ===")
        reply = await diagnose(path)
        print(reply)
        print()
        results[name] = reply

    out_path = os.path.join(os.path.dirname(__file__), "..", "logs", "gate1_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

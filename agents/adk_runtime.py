"""Shared one-turn Google ADK runner for the monitoring agents."""

import uuid
from typing import Any, Iterable, Optional

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

APP_NAME = "locustforge_monitoring"
USER_ID = "monitor_service"


async def run_agent(*, name: str, model: str, instruction: str, prompt: str,
                    tools: Optional[Iterable[Any]] = None) -> str:
    """Run one isolated ADK turn and return its final text response.

    ADK uses ``GOOGLE_API_KEY``/``GEMINI_API_KEY``, or configured Vertex AI
    credentials. Authentication stays outside application code.
    """
    sessions = InMemorySessionService()
    session = await sessions.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=str(uuid.uuid4())
    )
    agent = Agent(name=name, model=model, instruction=instruction, tools=list(tools or []))
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=sessions)
    result = ""
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        if event.is_final_response() and event.content:
            result = "".join(part.text or "" for part in event.content.parts if part.text)
    return result

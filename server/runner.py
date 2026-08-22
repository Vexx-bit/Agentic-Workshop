"""ADK runner wrapper: one persistent session per WhatsApp sender."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from browser_agent.agent import root_agent
from browser_agent.config import APP_NAME

logger = logging.getLogger(__name__)

_session_service = InMemorySessionService()
_runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=_session_service)

# The browser is a single shared resource; serialise turns so two WhatsApp
# users cannot drive the same Playwright context at the same time.
_browser_lock = asyncio.Lock()


def session_id_for(sender: str) -> str:
    """Stable, non-PII session id derived from the WhatsApp sender."""
    return hashlib.sha256(sender.encode("utf-8")).hexdigest()[:32]


async def run_turn(sender: str, text: str) -> str:
    """Runs one agent turn for a WhatsApp sender and returns the reply text."""
    sid = session_id_for(sender)

    session = await _session_service.get_session(
        app_name=APP_NAME, user_id=sid, session_id=sid
    )
    if session is None:
        session = await _session_service.create_session(
            app_name=APP_NAME, user_id=sid, session_id=sid
        )

    message = types.Content(role="user", parts=[types.Part(text=text)])

    chunks: list[str] = []
    async with _browser_lock:
        async for event in _runner.run_async(
            user_id=sid, session_id=sid, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        chunks.append(part.text)

    reply = "\n".join(c.strip() for c in chunks if c and c.strip()).strip()
    if not reply:
        reply = (
            "I finished that step but produced no text. Try rephrasing, or ask "
            "me what page I'm currently on."
        )
    return reply

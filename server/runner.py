"""ADK runner wrapper: one persistent session per WhatsApp sender.

Concurrency model
-----------------
Many students use this at once, so there is no process-wide lock any more:

- One lock per sender. A student's own messages stay strictly ordered, and two
  messages from the same person cannot interleave inside their session.
- One semaphore capping total in-flight turns, so a class hitting the bot at
  the same time queues instead of exhausting the container.
- Moodle work is stateless REST with a per-student token, so it parallelises
  cleanly. Twenty students can be served at once.

The browser toolset is the one genuinely shared resource: there is a single
Playwright MCP connection, so two simultaneous *browser* turns can land in the
same tab. The LMS path never touches the browser. Keep that in mind when
demoing the general-web capability alongside a live audience.

Session identity
----------------
The session id and the Moodle key are both derived from the sender's number by
hashing, never stored raw. The key is written into session state so Moodle
tools read it from the session rather than from a model argument - a model that
cannot name a key cannot ask for another student's coursework.
"""

from __future__ import annotations

import asyncio
import logging
import os

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from browser_agent.agent import root_agent
from browser_agent.config import APP_NAME
from browser_agent.moodle import USER_KEY_STATE
from browser_agent.store import user_key_for

logger = logging.getLogger(__name__)

_session_service = InMemorySessionService()
_runner = Runner(app_name=APP_NAME, agent=root_agent, session_service=_session_service)

MAX_CONCURRENT_TURNS = int(os.getenv("MAX_CONCURRENT_TURNS", "4"))
_turn_slots = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
_locks: dict[str, asyncio.Lock] = {}


def session_id_for(sender: str) -> str:
    """Stable, non-PII session id derived from the WhatsApp sender."""
    return user_key_for(sender)


def _lock_for(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


async def _session_for(sid: str):
    """Fetches or creates this student's session, carrying their opaque key."""
    session = await _session_service.get_session(
        app_name=APP_NAME, user_id=sid, session_id=sid
    )
    if session is None:
        try:
            session = await _session_service.create_session(
                app_name=APP_NAME,
                user_id=sid,
                session_id=sid,
                state={USER_KEY_STATE: sid},
            )
        except TypeError:
            # Older ADK signatures without a state kwarg.
            session = await _session_service.create_session(
                app_name=APP_NAME, user_id=sid, session_id=sid
            )

    # Sessions created before this key existed, or by a build without state
    # support, are repaired in place rather than dropped.
    try:
        if not session.state.get(USER_KEY_STATE):
            session.state[USER_KEY_STATE] = sid
    except Exception:
        logger.warning("could not set %s on session state", USER_KEY_STATE)
    return session


async def run_turn(sender: str, text: str) -> str:
    """Runs one agent turn for a WhatsApp sender and returns the reply text."""
    sid = session_id_for(sender)
    await _session_for(sid)

    message = types.Content(role="user", parts=[types.Part(text=text)])

    chunks: list[str] = []
    async with _turn_slots:
        async with _lock_for(sid):
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
            "I finished that step but produced no text. Try rephrasing, or send "
            "'help' to see what I can do."
        )
    return reply

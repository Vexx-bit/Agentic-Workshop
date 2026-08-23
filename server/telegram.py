"""Telegram Bot API outbound helper.

Why this exists: Twilio trial accounts reject free-form WhatsApp bodies
(HTTP 400, "trial accounts have limited parameter access") and only accept
pre-approved templates. Agent replies are arbitrary prose, so they cannot be
expressed as a template. Telegram has no template rules, no 24-hour session
window and no per-message cost, which makes it the reliable demo interface.

The agent, the token store and the LMS layer are untouched by this file. Only
transport differs.
"""

from __future__ import annotations

import logging

import httpx

from browser_agent.config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_MAX_CHARS,
    TELEGRAM_WEBHOOK_SECRET,
)
from server.whatsapp import chunk

logger = logging.getLogger(__name__)

# Built by concatenation: a literal https string in source has been mangled by
# URL rewriting before, and a broken API root fails at runtime, not at build.
_API_ROOT = "https:" + "//api.telegram.org/bot"

HTTP_TIMEOUT = 30


def configured() -> bool:
    """True when a bot token is present, so the channel can be advertised."""
    return bool(TELEGRAM_BOT_TOKEN)


def secret_ok(header_value: str) -> bool:
    """Authenticates an inbound update.

    Telegram echoes the secret we registered with setWebhook in the
    X-Telegram-Bot-Api-Secret-Token header. If no secret is configured we accept
    the update, which keeps local development workable; in production the
    secret should always be set.
    """
    if not TELEGRAM_WEBHOOK_SECRET:
        return True
    return header_value == TELEGRAM_WEBHOOK_SECRET


def _endpoint(method: str) -> str:
    return f"{_API_ROOT}{TELEGRAM_BOT_TOKEN}/{method}"


def send(chat_id: str, body: str) -> list[str]:
    """Sends a message (chunked). Returns the Telegram message ids."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment.")

    ids: list[str] = []
    for piece in chunk(body, TELEGRAM_MAX_CHARS):
        response = httpx.post(
            _endpoint("sendMessage"),
            json={
                "chat_id": chat_id,
                "text": piece,
                "disable_web_page_preview": True,
            },
            timeout=HTTP_TIMEOUT,
        )
        # Telegram returns 200 with ok:false for application errors, so check
        # both the status code and the payload.
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"telegram sendMessage failed: {payload}")
        ids.append(str(payload.get("result", {}).get("message_id", "")))

    logger.info("sent %d telegram message(s) to %s", len(ids), chat_id)
    return ids


def extract(update: dict) -> tuple[str, str]:
    """Pulls (chat_id, text) out of a Telegram update.

    Returns empty strings for updates we do not handle (edits, joins, stickers,
    channel posts), so the caller can ack and ignore them.
    """
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = (message.get("text") or "").strip()
    return chat_id, text

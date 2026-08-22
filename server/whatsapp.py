"""Twilio WhatsApp outbound helper."""

from __future__ import annotations

import logging

from browser_agent.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_FROM,
    WHATSAPP_MAX_CHARS,
)

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from twilio.rest import Client

        if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set in the environment."
            )
        _client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _client


def chunk(text: str, size: int = WHATSAPP_MAX_CHARS) -> list[str]:
    """Splits a reply into WhatsApp-safe chunks on paragraph/word boundaries."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= size:
            parts.append(remaining)
            break
        window = remaining[:size]
        split_at = max(window.rfind("\n"), window.rfind(". "), window.rfind(" "))
        if split_at < size // 2:
            split_at = size
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return [p for p in parts if p]


def send(to: str, body: str) -> list[str]:
    """Sends a WhatsApp message (chunked). Returns the Twilio message SIDs."""
    client = _get_client()
    sids: list[str] = []
    for piece in chunk(body):
        message = client.messages.create(
            from_=TWILIO_WHATSAPP_FROM, to=to, body=piece
        )
        sids.append(message.sid)
    logger.info("sent %d whatsapp message(s) to %s", len(sids), to)
    return sids

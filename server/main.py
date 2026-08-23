"""FastAPI ingress for the WhatsApp-operated study agent (Twilio).

Key constraint: Twilio's webhook expects a response within ~10-15s, and both
browser automation and multi-step LMS work are slower than that. So we ACK
immediately with empty TwiML and do the real work in a background task,
replying via the Twilio REST API.

Three routes matter:
    POST /whatsapp        Twilio webhook
    GET|POST /link/{id}   single-use page where a student links their own Moodle
    GET  /media/{id}      short-lived proxy for Moodle files

Health: GET /, /healthz and /healthz/ all return the same JSON. That
redundancy is deliberate - a bare base URL returning 404 reads exactly like a
dead deployment, and a trailing slash used to produce a 307 that curl silently
does not follow.

Run locally:
    uvicorn server.main:app --reload --port 8000
    ngrok http 8000
Then point the Twilio WhatsApp sandbox "WHEN A MESSAGE COMES IN" webhook at:
    https://<your-ngrok-domain>/whatsapp    (HTTP POST)
"""

from __future__ import annotations

import logging
import os

from fastapi import BackgroundTasks, FastAPI, Form, Request, Response

from browser_agent import moodle, store
from browser_agent.config import (
    PUBLIC_BASE_URL,
    TWILIO_AUTH_TOKEN,
    TWILIO_VALIDATE_SIGNATURE,
)
from server import whatsapp
from server.link import router as link_router
from server.media import router as media_router
from server.runner import run_turn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-browser-agent")

app = FastAPI(title="WhatsApp Study Agent")
app.include_router(media_router)
app.include_router(link_router)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

HELP_TEXT = (
    "Study assistant ready.\n\n"
    "First, send: link\n"
    "That gives you a private one-time page to sign in to your e-learning "
    "account. Your password is never typed into this chat.\n\n"
    "Then try:\n"
    "- what are my units\n"
    "- what is mobile programming about now\n"
    "- send me the assignment questions for mobile programming\n"
    "- what's due in the next 2 weeks\n"
    "- remind me on Friday 6pm to finish the lab\n\n"
    "I will not submit coursework, sit a quiz or touch a grade - that is "
    "blocked in code. I fetch the questions and notes; you do the work.\n\n"
    "Send unlink at any time to delete your stored access."
)

LINK_WORDS = {"link", "link me", "connect", "login", "log in", "sign in"}
UNLINK_WORDS = {"unlink", "forget me", "logout", "log out", "disconnect", "delete my data"}


def _validate_signature(request: Request, form: dict) -> bool:
    if not TWILIO_VALIDATE_SIGNATURE:
        return True
    try:
        from twilio.request_validator import RequestValidator
    except Exception:
        logger.warning("twilio validator unavailable; skipping signature check")
        return True

    signature = request.headers.get("X-Twilio-Signature", "")
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    url = f"{base}{request.url.path}"
    return RequestValidator(TWILIO_AUTH_TOKEN).validate(url, form, signature)


def _send_link(sender: str) -> None:
    """Mints and sends a link page. Deterministic: no model in the path.

    Linking is the one step where a confused model would strand a student, so
    the plain word 'link' is handled here rather than as a tool call.
    """
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        whatsapp.send(sender, "Linking isn't configured on this deployment yet.")
        return
    nonce = store.new_link_nonce(store.user_key_for(sender))
    minutes = max(1, store.LINK_TTL_SECONDS // 60)
    whatsapp.send(
        sender,
        "Open this to sign in to your e-learning account:\n"
        f"{base}/link/{nonce}\n\n"
        f"It works once and expires in {minutes} minutes. Your password is "
        "exchanged for an access token and is never saved, logged, or typed "
        "into this chat.",
    )


def _do_unlink(sender: str) -> None:
    was_linked = moodle.forget_token(store.user_key_for(sender))
    whatsapp.send(
        sender,
        "Done - your stored e-learning access has been deleted. Send 'link' if "
        "you want to connect again."
        if was_linked
        else "You weren't linked, so there was nothing to delete.",
    )


async def _handle(sender: str, body: str) -> None:
    """Background worker: run the agent, then reply over WhatsApp."""
    try:
        reply = await run_turn(sender, body)
    except Exception as exc:  # never leave the user hanging
        logger.exception("agent turn failed")
        reply = (
            "Something broke while working on that: "
            f"{type(exc).__name__}. Try again with a simpler request, or send "
            "'help' to see what I can do."
        )

    try:
        whatsapp.send(sender, reply)
    except Exception:
        logger.exception("failed to send whatsapp reply to %s", sender)


def _health() -> dict:
    """Everything needed to tell a live deploy from a stale one, in one GET.

    link_store is the load-bearing field: it proves the token store actually
    initialised, which is what decides whether students stay linked.
    """
    return {
        "status": "ok",
        "service": "whatsapp-study-agent",
        "link_store": store.backend_name(),
        "revision": os.getenv("K_REVISION", "local"),
        "linking_configured": bool((PUBLIC_BASE_URL or "").strip()),
    }


# Three spellings, one handler. A bare base URL must never look dead.
@app.get("/")
async def root() -> dict:
    return _health()


@app.get("/healthz")
async def healthz() -> dict:
    return _health()


@app.get("/healthz/")
async def healthz_slash() -> dict:
    return _health()


@app.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background: BackgroundTasks,
    From: str = Form(default=""),
    Body: str = Form(default=""),
) -> Response:
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}

    if not _validate_signature(request, form):
        logger.warning("rejected webhook with bad Twilio signature")
        return Response(status_code=403, content="invalid signature")

    sender = From or form.get("From", "")
    body = (Body or form.get("Body", "")).strip()
    logger.info("inbound from %s: %s", sender, body[:200])

    if not sender:
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    lowered = body.lower()

    if lowered in {"hi", "hello", "help", "start", "/start", "menu"}:
        background.add_task(whatsapp.send, sender, HELP_TEXT)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    if lowered in LINK_WORDS:
        background.add_task(_send_link, sender)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    if lowered in UNLINK_WORDS:
        background.add_task(_do_unlink, sender)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    # Never accept credentials over chat, even if offered unprompted.
    if "password" in lowered and (":" in body or "=" in body):
        background.add_task(
            whatsapp.send,
            sender,
            "Don't send passwords here - WhatsApp keeps this chat. Send 'link' "
            "and type it once on the secure page instead.",
        )
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    if not body:
        background.add_task(
            whatsapp.send, sender, "Send me a text instruction (media isn't supported yet)."
        )
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    # Fast ack + async work: this is the non-negotiable timeout rule.
    background.add_task(whatsapp.send, sender, "On it\u2026")
    background.add_task(_handle, sender, body)
    return Response(content=EMPTY_TWIML, media_type="application/xml")

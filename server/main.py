"""FastAPI ingress for the study agent (Twilio WhatsApp + Telegram).

Key constraint: a messaging webhook expects a response within seconds, and both
browser automation and multi-step LMS work are slower than that. So we ACK
immediately and do the real work in a background task, replying over the API.

Two interfaces, one agent:
    POST /whatsapp        Twilio webhook (needs a non-trial Twilio account:
                          trial accounts reject free-form bodies with HTTP 400
                          "trial accounts have limited parameter access")
    POST /telegram        Telegram webhook (free, no templates, no 24h window)

Shared routes:
    GET|POST /link/{id}   single-use page where a student links their own Moodle
    GET  /media/{id}      short-lived proxy for Moodle files

The channel is carried in the sender key: "whatsapp:+254..." or
"telegram:<chat_id>". Every downstream layer (store keys, ADK sessions,
per-sender locks) is keyed off that string, so the two channels stay isolated
from each other and per student.

Health: GET /, /healthz and /healthz/ all return the same JSON. That
redundancy is deliberate - a bare base URL returning 404 reads exactly like a
dead deployment, and a trailing slash used to produce a 307 that curl silently
does not follow.

Run locally:
    uvicorn server.main:app --reload --port 8000
    ngrok http 8000
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
from server import telegram, whatsapp
from server.link import router as link_router
from server.media import router as media_router
from server.runner import run_turn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-browser-agent")

app = FastAPI(title="Study Agent (WhatsApp + Telegram)")
app.include_router(media_router)
app.include_router(link_router)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

TELEGRAM_PREFIX = "telegram:"

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
GREETINGS = {"hi", "hello", "help", "start", "/start", "menu"}


def _reply(sender: str, text: str) -> None:
    """Sends text back over whichever channel the sender came from.

    One dispatch point, so no caller has to know about transports.
    """
    if sender.startswith(TELEGRAM_PREFIX):
        telegram.send(sender[len(TELEGRAM_PREFIX):], text)
    else:
        whatsapp.send(sender, text)


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
        _reply(sender, "Linking isn't configured on this deployment yet.")
        return
    nonce = store.new_link_nonce(store.user_key_for(sender))
    minutes = max(1, store.LINK_TTL_SECONDS // 60)
    _reply(
        sender,
        "Open this to sign in to your e-learning account:\n"
        f"{base}/link/{nonce}\n\n"
        f"It works once and expires in {minutes} minutes. Your password is "
        "exchanged for an access token and is never saved, logged, or typed "
        "into this chat.",
    )


def _do_unlink(sender: str) -> None:
    was_linked = moodle.forget_token(store.user_key_for(sender))
    _reply(
        sender,
        "Done - your stored e-learning access has been deleted. Send 'link' if "
        "you want to connect again."
        if was_linked
        else "You weren't linked, so there was nothing to delete.",
    )


async def _handle(sender: str, body: str) -> None:
    """Background worker: run the agent, then reply on the sender's channel."""
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
        _reply(sender, reply)
    except Exception:
        logger.exception("failed to send reply to %s", sender)


def _dispatch(sender: str, body: str, background: BackgroundTasks) -> None:
    """Channel-agnostic command routing. Queues work; never blocks."""
    lowered = body.lower()

    if lowered in GREETINGS:
        background.add_task(_reply, sender, HELP_TEXT)
        return

    if lowered in LINK_WORDS:
        background.add_task(_send_link, sender)
        return

    if lowered in UNLINK_WORDS:
        background.add_task(_do_unlink, sender)
        return

    # Never accept credentials over chat, even if offered unprompted.
    if "password" in lowered and (":" in body or "=" in body):
        background.add_task(
            _reply,
            sender,
            "Don't send passwords here - this chat is stored. Send 'link' and "
            "type it once on the secure page instead.",
        )
        return

    if not body:
        background.add_task(
            _reply, sender, "Send me a text instruction (media isn't supported yet)."
        )
        return

    # Fast ack + async work: this is the non-negotiable timeout rule.
    background.add_task(_reply, sender, "On it\u2026")
    background.add_task(_handle, sender, body)


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
        "telegram_configured": telegram.configured(),
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
    logger.info("inbound whatsapp from %s: %s", sender, body[:200])

    if sender:
        _dispatch(sender, body, background)

    return Response(content=EMPTY_TWIML, media_type="application/xml")


@app.post("/telegram")
async def telegram_webhook(request: Request, background: BackgroundTasks) -> dict:
    """Telegram webhook.

    Authenticated by the secret token Telegram echoes back in a header, which is
    the Telegram counterpart to Twilio's request signature. Always returns 200
    with a short body: a non-2xx makes Telegram retry the same update.
    """
    if not telegram.secret_ok(request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")):
        logger.warning("rejected telegram update with bad secret token")
        return {"ok": False}

    try:
        update = await request.json()
    except Exception:
        logger.warning("telegram update was not valid JSON")
        return {"ok": True}

    chat_id, text = telegram.extract(update)
    if not chat_id:
        return {"ok": True}

    sender = f"{TELEGRAM_PREFIX}{chat_id}"
    logger.info("inbound telegram from %s: %s", sender, text[:200])
    _dispatch(sender, text, background)
    return {"ok": True}

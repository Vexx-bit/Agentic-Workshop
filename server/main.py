"""FastAPI ingress for the study agent (Twilio WhatsApp + Telegram).

Why the two channels reply differently
-------------------------------------
Twilio trial accounts refuse free-form message bodies on the REST API:
Messages.json answers 400 `21654 ContentSid Required` for any `body`, on every
WhatsApp sender including the sandbox. Verified with a raw curl, so it is an
account-level restriction, not a sender or code problem.

A TwiML response returned *from the webhook* is not an API create, so it is not
subject to that restriction. WhatsApp therefore answers inline, in the HTTP
response to the inbound webhook.

The cost of that is real and worth naming: the reply has to be ready before
Twilio's webhook window closes. So an agent turn is raced against a budget. If
it wins, the student gets the answer immediately. If it loses, we ack, let the
task finish in the background, and stash the result - the student pulls it with
`more`. Nothing is lost, nothing hangs.

Telegram has no such restriction, so it keeps the cleaner ack-now/reply-later
API path.

Routes
------
    POST /whatsapp        Twilio webhook (replies with TwiML)
    POST /telegram        Telegram webhook (replies over the Bot API)
    GET|POST /link/{id}   single-use page where a student links their own Moodle
    GET  /media/{id}      short-lived proxy for Moodle files

The channel is carried in the sender key: "whatsapp:+254..." or
"telegram:<chat_id>". Every downstream layer (store keys, ADK sessions,
per-sender locks) is keyed off that string, so the two channels stay isolated
from each other and per student.

Health: GET /, /healthz and /healthz/ all return the same JSON.

Run locally:
    uvicorn server.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
from xml.sax.saxutils import escape as xml_escape

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

TELEGRAM_PREFIX = "telegram:"

# Twilio gives a webhook roughly 15s before it gives up. Stay clearly inside it.
TWIML_BUDGET_SECONDS = float(os.getenv("TWIML_BUDGET_SECONDS", "11"))

# Twilio accepts several <Message> verbs per response; a handful keeps the
# payload small while still delivering most answers in one shot.
MAX_TWIML_MESSAGES = 3

# Answers that did not fit the budget or the message cap, per sender.
# In-memory is correct here: the service runs single-instance by design.
_PENDING: dict[str, list[str]] = {}

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
MORE_WORDS = {"more", "next", "continue", "go on", "?"}

STILL_WORKING = (
    "Working on it - that one needs a few more seconds (reading your unit "
    "material). Send: more"
)


# --------------------------------------------------------------------------- #
# TwiML
# --------------------------------------------------------------------------- #


def _twiml(messages: list[str]) -> Response:
    """Wraps zero or more message bodies in a TwiML response."""
    body = "".join(f"<Message>{xml_escape(m)}</Message>" for m in messages if m)
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?>' f"<Response>{body}</Response>",
        media_type="application/xml",
    )


def _twiml_text(sender: str, text: str) -> Response:
    """Chunks text for WhatsApp, sends what fits, queues the rest for 'more'."""
    parts = whatsapp.chunk(text)
    if not parts:
        return _twiml([])

    head = parts[:MAX_TWIML_MESSAGES]
    tail = parts[MAX_TWIML_MESSAGES:]
    if tail:
        _PENDING.setdefault(sender, []).extend(tail)
        head = head[:-1] + [head[-1] + f"\n\n(...{len(tail)} more - send: more)"]
    return _twiml(head)


def _pop_pending(sender: str) -> list[str]:
    queue = _PENDING.get(sender) or []
    if not queue:
        return []
    taken, rest = queue[:MAX_TWIML_MESSAGES], queue[MAX_TWIML_MESSAGES:]
    if rest:
        _PENDING[sender] = rest
        taken = taken[:-1] + [taken[-1] + f"\n\n(...{len(rest)} more - send: more)"]
    else:
        _PENDING.pop(sender, None)
    return taken


# --------------------------------------------------------------------------- #
# Deterministic commands (no model in the path)
# --------------------------------------------------------------------------- #


def _link_text(sender: str) -> str:
    """Mints a one-time link page. Deterministic: a confused model here would
    strand a student, so the plain word 'link' never reaches the agent."""
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        return "Linking isn't configured on this deployment yet."
    nonce = store.new_link_nonce(store.user_key_for(sender))
    minutes = max(1, store.LINK_TTL_SECONDS // 60)
    return (
        "Open this to sign in to your e-learning account:\n"
        f"{base}/link/{nonce}\n\n"
        f"It works once and expires in {minutes} minutes. Your password is "
        "exchanged for an access token and is never saved, logged, or typed "
        "into this chat."
    )


def _unlink_text(sender: str) -> str:
    if moodle.forget_token(store.user_key_for(sender)):
        return (
            "Done - your stored e-learning access has been deleted. Send 'link' "
            "if you want to connect again."
        )
    return "You weren't linked, so there was nothing to delete."


def _fast_text(sender: str, body: str) -> str | None:
    """Returns a reply for commands that need no agent turn, else None."""
    lowered = body.lower()

    if lowered in GREETINGS:
        return HELP_TEXT
    if lowered in LINK_WORDS:
        return _link_text(sender)
    if lowered in UNLINK_WORDS:
        return _unlink_text(sender)
    # Never accept credentials over chat, even if offered unprompted.
    if "password" in lowered and (":" in body or "=" in body):
        return (
            "Don't send passwords here - this chat is stored. Send 'link' and "
            "type it once on the secure page instead."
        )
    if not body:
        return "Send me a text instruction (media isn't supported yet)."
    return None


def _failure_text(exc: BaseException) -> str:
    return (
        f"Something broke while working on that: {type(exc).__name__}. Try again "
        "with a simpler request, or send 'help' to see what I can do."
    )


# --------------------------------------------------------------------------- #
# WhatsApp: answer inside the webhook response
# --------------------------------------------------------------------------- #


async def _agent_text(sender: str, body: str) -> str | None:
    """Races an agent turn against the webhook budget.

    Returns the answer if it finished in time. Otherwise returns None and lets
    the task keep running, stashing its result for the next 'more'.
    """
    task = asyncio.create_task(run_turn(sender, body))
    done, _pending = await asyncio.wait({task}, timeout=TWIML_BUDGET_SECONDS)

    if task in done:
        try:
            return task.result()
        except Exception as exc:
            logger.exception("agent turn failed")
            return _failure_text(exc)

    def _stash(finished: asyncio.Task) -> None:
        try:
            text = finished.result()
        except Exception as exc:  # noqa: BLE001 - reported to the student
            logger.exception("agent turn failed after the webhook returned")
            text = _failure_text(exc)
        _PENDING.setdefault(sender, []).extend(whatsapp.chunk(text))
        logger.info("queued a late answer for %s", sender)

    task.add_done_callback(_stash)
    logger.info("turn for %s exceeded the twiml budget; queued", sender)
    return None


# --------------------------------------------------------------------------- #
# Telegram: ack now, reply over the API
# --------------------------------------------------------------------------- #


def _reply(sender: str, text: str) -> None:
    if sender.startswith(TELEGRAM_PREFIX):
        telegram.send(sender[len(TELEGRAM_PREFIX):], text)
    else:
        whatsapp.send(sender, text)


async def _handle(sender: str, body: str) -> None:
    try:
        reply = await run_turn(sender, body)
    except Exception as exc:  # never leave the user hanging
        logger.exception("agent turn failed")
        reply = _failure_text(exc)
    try:
        _reply(sender, reply)
    except Exception:
        logger.exception("failed to send reply to %s", sender)


def _dispatch(sender: str, body: str, background: BackgroundTasks) -> None:
    fast = _fast_text(sender, body)
    if fast is not None:
        background.add_task(_reply, sender, fast)
        return
    background.add_task(_reply, sender, "On it...")
    background.add_task(_handle, sender, body)


# --------------------------------------------------------------------------- #
# Signature check
# --------------------------------------------------------------------------- #


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
    ok = RequestValidator(TWILIO_AUTH_TOKEN).validate(url, form, signature)
    if not ok:
        # Log what we signed against - a scheme or host mismatch is invisible
        # otherwise, and this check has already cost hours once.
        logger.warning(
            "signature mismatch: signed_url=%r fields=%d has_header=%s",
            url,
            len(form),
            bool(signature),
        )
    return ok


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def _health() -> dict:
    return {
        "status": "ok",
        "service": "whatsapp-study-agent",
        "link_store": store.backend_name(),
        "revision": os.getenv("K_REVISION", "local"),
        "linking_configured": bool((PUBLIC_BASE_URL or "").strip()),
        "telegram_configured": telegram.configured(),
        "whatsapp_reply_mode": "twiml",
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

    if not sender:
        return _twiml([])

    if body.lower() in MORE_WORDS:
        queued = _pop_pending(sender)
        if queued:
            return _twiml(queued)
        return _twiml_text(sender, "Nothing queued. Ask me something.")

    fast = _fast_text(sender, body)
    if fast is not None:
        return _twiml_text(sender, fast)

    answer = await _agent_text(sender, body)
    if answer is None:
        return _twiml_text(sender, STILL_WORKING)
    return _twiml_text(sender, answer)


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

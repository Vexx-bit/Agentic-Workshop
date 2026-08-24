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
Twilio's webhook window closes. So the work is raced against a budget. If it
wins, the student gets the answer immediately. If it loses, we ack, let the task
finish in the background, and stash the result - the student pulls it with
`more`. Nothing is lost, nothing hangs.

A queued answer is labelled with what produced it. Without that label a late
answer arrives after the student has already asked something else, and reads as
though the assistant ignored the question - which is exactly how it looked in
testing.

Three kinds of question
-----------------------
Text, voice note, and photo/document. Media is read by Gemini in server/intake.py
and then answered by the same agent, so the microphone and the camera are
first-class inputs rather than an afterthought - on WhatsApp they are how people
actually communicate.

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
from typing import Awaitable
from xml.sax.saxutils import escape as xml_escape

from fastapi import BackgroundTasks, FastAPI, Form, Request, Response

from browser_agent import moodle, store
from browser_agent.config import (
    PUBLIC_BASE_URL,
    TWILIO_AUTH_TOKEN,
    TWILIO_VALIDATE_SIGNATURE,
)
from server import format as fmt
from server import intake, telegram, whatsapp
from server.link import router as link_router
from server.media import router as media_router
from server.runner import run_turn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-browser-agent")

app = FastAPI(title="Study Agent (WhatsApp + Telegram)")
app.include_router(media_router)
app.include_router(link_router)

TELEGRAM_PREFIX = "telegram:"

# Twilio gives a webhook roughly 15s before it gives up. Stay inside it, but do
# not leave seconds on the table: at 11s every single turn in testing overran
# and every answer needed a follow-up 'more', which reads as a broken bot.
TWIML_BUDGET_SECONDS = float(os.getenv("TWIML_BUDGET_SECONDS", "13"))

# Twilio accepts several <Message> verbs per response; a handful keeps the
# payload small while still delivering most answers in one shot.
MAX_TWIML_MESSAGES = 3

# Answers that did not fit the budget or the message cap, per sender.
# In-memory is correct here: the service runs single-instance by design.
_PENDING: dict[str, list[str]] = {}

# What each sender is currently waiting on, so 'more' can say what is still
# running instead of claiming nothing is queued.
_INFLIGHT: dict[str, str] = {}

WELCOME_TEXT = (
    "Hi - I'm your study assistant, here on WhatsApp.\n\n"
    "I read your own e-learning account and answer in plain language: what a "
    "topic covers, what the notes say, what an assignment actually asks for, "
    "what is due.\n\n"
    "*Talk to me normally.* Full sentences, your own words - or hold the mic "
    "and just ask out loud.\n\n"
    "To get started, send: link\n\n"
    "That opens a private one-time page where you sign in. Your password is "
    "never typed into this chat. Send help any time to see everything I can do."
)

HELP_TEXT = (
    "*Just talk to me normally.*\n"
    "I'm a Gemini agent, not a command line - ask however you'd ask a "
    "classmate. Full sentences, short phrases, English or Kiswahili.\n\n"
    "*You can also just send:*\n"
    "- a voice note - ask out loud, I'll listen and answer\n"
    "- a photo of a question, a slide or the whiteboard\n"
    "- a PDF or document to read and explain\n\n"
    "*Things to ask*\n"
    "- what is <unit> about now\n"
    "- send me the assignment questions for <unit>\n"
    "- explain <topic> from our notes\n"
    "- what notes are there for <unit>\n"
    "- what have I not finished in <unit>\n"
    "- what should I work on today\n"
    "- remind me on Friday 6pm to finish the lab\n\n"
    "Use any unit you take, by code or by name - I read your units from your "
    "own account, whatever course you're on.\n\n"
    "*Shortcuts, if you'd rather tap a number*\n"
    "1 link  2 my units  3 what's due  4 my progress\n"
    "5 help  6 privacy  7 status  8 unlink\n\n"
    "Capitals don't matter. If something takes a few seconds I'll say so - "
    "send more and it'll be waiting.\n\n"
    "I fetch and explain. I never submit coursework, sit a quiz or touch a "
    "grade - that's blocked in code, not just discouraged."
)

PRIVACY_TEXT = (
    "*What I store*\n\n"
    "- Your password: never. You type it once on the one-time page, it is "
    "swapped for an access token, and it is never saved or logged.\n"
    "- Your phone number: kept only as a one-way hash, so I can find your "
    "access again without being able to read your number back out.\n"
    "- Your access token: held for this service only, and deleted the moment "
    "you send unlink.\n"
    "- Voice notes and photos: read in memory to answer you, never written to "
    "disk or kept.\n"
    "- This chat: WhatsApp keeps your message history, which is exactly why "
    "your password never goes in here.\n"
    "- Your coursework: read only. Submitting, uploading, attempting a quiz "
    "and changing a grade are blocked in code.\n\n"
    "Nobody else's account is reachable from your chat, and yours is not "
    "reachable from theirs.\n\n"
    "Send unlink at any time."
)

LINK_WORDS = {"link", "link me", "connect", "login", "log in", "sign in"}
UNLINK_WORDS = {"unlink", "forget me", "logout", "log out", "disconnect", "delete my data"}
GREETINGS = {"hi", "hey", "hello", "help", "start", "/start", "menu", "commands"}
MORE_WORDS = {"more", "next", "continue", "go on", "?"}
PRIVACY_WORDS = {"privacy", "is it safe", "safety", "what do you store", "security"}
STATUS_WORDS = {"status", "am i linked", "linked"}

# Numbered shortcuts. Everything here is also reachable in plain language - the
# numbers exist for speed and for anyone who does not want to type, never as the
# only way in.
NUMBER_SHORTCUTS = {
    "1": "link",
    "2": "what are my units",
    "3": "what is due in the next two weeks",
    "4": "how far am I in each of my units",
    "5": "help",
    "6": "privacy",
    "7": "status",
    "8": "unlink",
}

STILL_WORKING = (
    "Working on that now - it takes a few seconds when I have to read your "
    "unit material.\n\nSend: more"
)

MEDIA_WORKING = {
    "voice": "Listening to your voice note now - a few seconds.\n\nSend: more",
    "image": "Reading your photo now - a few seconds.\n\nSend: more",
    "document": "Reading that file now - a few seconds.\n\nSend: more",
}

MEDIA_LABELS = {
    "voice": "your voice note",
    "image": "your photo",
    "document": "your file",
}


def _normalise(body: str) -> str:
    """Lowercases and strips trailing punctuation for command matching.

    Students type 'Help!', 'link.' and 'MORE'. None of those should miss.
    """
    return body.strip().lower().strip(" .!?,;:")


def _short(text: str, limit: int = 60) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "\u2026"


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


def _render(sender: str, text: str, budget: int = MAX_TWIML_MESSAGES) -> list[str]:
    """Formats and chunks text, returning what fits and queueing the rest."""
    if budget < 1:
        budget = 1
    parts = whatsapp.chunk(fmt.for_chat(text))
    if not parts:
        return []

    head, tail = parts[:budget], parts[budget:]
    if tail:
        _PENDING.setdefault(sender, []).extend(tail)
        head = head[:-1] + [head[-1] + f"\n\n(...{len(tail)} more - send: more)"]
    return head


def _twiml_text(sender: str, text: str) -> Response:
    return _twiml(_render(sender, text))


def _pop_pending(sender: str, limit: int = MAX_TWIML_MESSAGES) -> list[str]:
    queue = _PENDING.get(sender) or []
    if not queue:
        return []
    taken, rest = queue[:limit], queue[limit:]
    if rest:
        _PENDING[sender] = rest
        taken = taken[:-1] + [taken[-1] + f"\n\n(...{len(rest)} more - send: more)"]
    else:
        _PENDING.pop(sender, None)
    return taken


# --------------------------------------------------------------------------- #
# Deterministic commands (no model in the path)
# --------------------------------------------------------------------------- #


def _is_linked(sender: str) -> bool:
    """Best-effort check, used only to pick between welcome and menu text.

    Deliberately tolerant: a wrong guess costs a slightly off greeting, so it
    must never raise and never block a real command.
    """
    key = store.user_key_for(sender)
    for name in ("has_token", "token_for", "get_token"):
        probe = getattr(moodle, name, None)
        if callable(probe):
            try:
                return bool(probe(key))
            except Exception:
                return False
    return False


def _link_text(sender: str) -> str:
    """Mints a one-time link page. Deterministic: a confused model here would
    strand a student, so the plain word 'link' never reaches the agent."""
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        return "Linking isn't configured on this deployment yet."
    nonce = store.new_link_nonce(store.user_key_for(sender))
    minutes = max(1, store.LINK_TTL_SECONDS // 60)
    return (
        "Tap to sign in to your e-learning account:\n"
        f"{base}/link/{nonce}\n\n"
        f"It works once and expires in {minutes} minutes. Your password is "
        "exchanged for an access token on that page and is never saved, "
        "logged, or typed into this chat.\n\n"
        "When it says you're linked, come back and ask me anything - or send a "
        "voice note."
    )


def _unlink_text(sender: str) -> str:
    if moodle.forget_token(store.user_key_for(sender)):
        return (
            "Done - your stored e-learning access has been deleted. Send 'link' "
            "if you want to connect again."
        )
    return "You weren't linked, so there was nothing to delete."


def _status_text(sender: str) -> str:
    if _is_linked(sender):
        return (
            "You're linked. Ask me anything about your units - or send a voice "
            "note or a photo of a question.\n\n"
            "Send unlink to delete my access at any time."
        )
    return "You're not linked yet. Send 'link' and sign in on the one-time page."


def _fast_text(sender: str, body: str) -> str | None:
    """Returns a reply for commands that need no agent turn, else None."""
    lowered = _normalise(body)

    if lowered in GREETINGS:
        # A first-time sender gets oriented; a linked student gets the menu.
        return HELP_TEXT if _is_linked(sender) else WELCOME_TEXT
    if lowered in LINK_WORDS:
        return _link_text(sender)
    if lowered in UNLINK_WORDS:
        return _unlink_text(sender)
    if lowered in PRIVACY_WORDS:
        return PRIVACY_TEXT
    if lowered in STATUS_WORDS:
        return _status_text(sender)
    # Never accept credentials over chat, even if offered unprompted.
    if "password" in lowered and (":" in body or "=" in body):
        return (
            "Don't send passwords here - this chat is stored on your phone and "
            "by the messaging provider. Send 'link' and type it once on the "
            "secure page instead."
        )
    if not body.strip():
        return "Send me a question - typed, spoken, or a photo."
    return None


def _failure_text(exc: BaseException) -> str:
    return (
        f"Something broke while working on that: {type(exc).__name__}. Try again "
        "with a simpler request, or send 'help' to see what I can do."
    )


# --------------------------------------------------------------------------- #
# WhatsApp: answer inside the webhook response
# --------------------------------------------------------------------------- #


async def _race(sender: str, label: str, work: Awaitable[str]) -> str | None:
    """Races one unit of work against the webhook budget.

    Returns the answer if it finished in time. Otherwise returns None and lets
    the task keep running, stashing its labelled result for the next 'more'.
    """
    task = asyncio.create_task(work)
    done, _pending = await asyncio.wait({task}, timeout=TWIML_BUDGET_SECONDS)

    if task in done:
        try:
            return task.result()
        except Exception as exc:
            logger.exception("turn failed")
            return _failure_text(exc)

    def _stash(finished: asyncio.Task) -> None:
        _INFLIGHT.pop(sender, None)
        try:
            text = finished.result()
        except Exception as exc:  # noqa: BLE001 - reported to the student
            logger.exception("turn failed after the webhook returned")
            text = _failure_text(exc)
        # Label it: by the time this is collected the student may have asked
        # something else, and an unlabelled answer looks like a non-answer.
        _PENDING.setdefault(sender, []).extend(
            whatsapp.chunk(fmt.for_chat(f"*Re:* {label}\n\n{text}"))
        )
        logger.info("queued a late answer for %s", sender)

    _INFLIGHT[sender] = label
    task.add_done_callback(_stash)
    logger.info("work for %s exceeded the twiml budget; queued", sender)
    return None


async def _media_answer(
    sender: str, caption: str, url: str, content_type: str
) -> str:
    """Reads inbound media with Gemini, then answers it as a question.

    The fetch and the model call are blocking, so they run in a worker thread:
    one student's voice note must not stall everyone else's turns.
    """
    result = await asyncio.to_thread(intake.read_media, url, content_type)
    status = result.get("status")
    kind = result.get("kind") or "document"

    if status == "unsupported":
        return (
            "I can read voice notes, photos, and PDF or text files. That format "
            "I can't - send it as a photo, or type your question."
        )
    if status == "empty":
        if kind == "voice":
            return (
                "I couldn't hear anything in that voice note. Try again "
                "somewhere quieter, or type your question."
            )
        return "I couldn't read anything in that. Try a clearer photo or file."
    if status != "success":
        logger.warning("media intake failed: %s", result.get("error_message"))
        return (
            "I couldn't open that one. Send it again, or type your question "
            "instead."
        )

    extracted = result.get("text", "").strip()
    caption = (caption or "").strip()

    if kind == "voice":
        # The transcript IS the question. Echo it: answering a misheard
        # question without showing what was heard is deeply confusing.
        question = f"{extracted}\n\n{caption}".strip() if caption else extracted
        prefix = f'\U0001f399 *Heard:* "{_short(extracted, 140)}"\n\n'
    else:
        noun = "photo" if kind == "image" else "file"
        question = (
            f"The student sent a {noun}. This is what it contains:\n\n"
            f"{extracted}\n\n"
            + (f"Their message with it: {caption}\n\n" if caption else "")
            + "Answer them about it, grounded in their own unit material where "
            "that is relevant. If it is coursework, explain how to approach it "
            "and what the question is really asking - never write the "
            "submission for them."
        )
        prefix = ""

    answer = await run_turn(sender, question)
    return prefix + answer


async def _agent_text(sender: str, body: str) -> str | None:
    return await _race(sender, f'"{_short(body)}"', run_turn(sender, body))


# --------------------------------------------------------------------------- #
# Telegram: ack now, reply over the API
# --------------------------------------------------------------------------- #


def _reply(sender: str, text: str) -> None:
    body = fmt.for_chat(text)
    if sender.startswith(TELEGRAM_PREFIX):
        telegram.send(sender[len(TELEGRAM_PREFIX):], body)
    else:
        whatsapp.send(sender, body)


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
    resolved = NUMBER_SHORTCUTS.get(_normalise(body), body)
    fast = _fast_text(sender, resolved)
    if fast is not None:
        background.add_task(_reply, sender, fast)
        return
    background.add_task(_reply, sender, "On it...")
    background.add_task(_handle, sender, resolved)


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
        "reply_budget_seconds": TWIML_BUDGET_SECONDS,
        "media_intake": ["voice", "image", "document"],
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

    try:
        num_media = int(form.get("NumMedia", "0") or 0)
    except ValueError:
        num_media = 0
    media_url = form.get("MediaUrl0", "")
    media_type = form.get("MediaContentType0", "")

    logger.info(
        "inbound whatsapp from %s: %s%s",
        sender,
        body[:200],
        f" [+{num_media} media: {media_type}]" if num_media else "",
    )

    if not sender:
        return _twiml([])

    lowered = _normalise(body)

    if not num_media and lowered in MORE_WORDS:
        queued = _pop_pending(sender)
        if queued:
            return _twiml(queued)
        waiting = _INFLIGHT.get(sender)
        if waiting:
            return _twiml_text(
                sender,
                f"Still working on {waiting} - give it a few more seconds, "
                "then send: more",
            )
        return _twiml_text(
            sender, "Nothing waiting. Ask me anything, or send help for ideas."
        )

    # Hand over any answer that finished after the last reply, before answering
    # this one - otherwise it surfaces later, out of order, looking like a
    # reply to the wrong question.
    lead = _pop_pending(sender, limit=1)

    # A voice note, photo or document is a question like any other.
    if num_media and media_url:
        kind = intake.kind_of(media_type) or "document"
        answer = await _race(
            sender,
            MEDIA_LABELS.get(kind, "your file"),
            _media_answer(sender, body, media_url, media_type),
        )
        text = MEDIA_WORKING.get(kind, STILL_WORKING) if answer is None else answer
        return _twiml(lead + _render(sender, text, MAX_TWIML_MESSAGES - len(lead)))

    # Numbered menu shortcuts become the plain-language request they describe.
    resolved = NUMBER_SHORTCUTS.get(lowered, body)

    fast = _fast_text(sender, resolved)
    if fast is not None:
        return _twiml(lead + _render(sender, fast, MAX_TWIML_MESSAGES - len(lead)))

    answer = await _agent_text(sender, resolved)
    text = STILL_WORKING if answer is None else answer
    return _twiml(lead + _render(sender, text, MAX_TWIML_MESSAGES - len(lead)))


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

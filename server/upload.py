"""A one-time page where a student sends a voice note, photo or file.

Why this exists
---------------
Twilio refuses to release inbound media on a trial account. The media resource
answers 20003 ("This feature is not available on a Trial account") while the
same credentials read Messages.json fine, so MediaUrl0 is unreachable no matter
what the code does.

Dropping voice and photo input was the alternative, and it was the wrong trade:
on WhatsApp the microphone and the camera are how people actually communicate,
and reading them is the part of this product that most needs Gemini. So the
media skips Twilio altogether. The student taps a link, records or photographs
on that page, and the bytes arrive here over HTTPS.

The pattern is already proven in this codebase - it is how Moodle linking works,
and it demos well because the handoff between chat and browser is visible.

The answer is rendered on the page AND queued for the chat, because the student
is looking at the browser at that moment, but the conversation lives in WhatsApp.

Safety
------
Tickets are unguessable, single-student, expiring and use-limited. The page
never displays the phone number, so a leaked link discloses nothing about who it
belongs to. Uploaded bytes are read in memory and never written to disk.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from html import escape

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import HTMLResponse

from browser_agent.config import PUBLIC_BASE_URL
from server import intake, outbox
from server.runner import run_turn

logger = logging.getLogger(__name__)

router = APIRouter()

TTL_SECONDS = int(os.getenv("UPLOAD_TTL_SECONDS", "900"))
# A student who mis-records should not have to go back to the chat for a new
# link, but the ticket must not become a permanent inbox either.
MAX_USES = int(os.getenv("UPLOAD_MAX_USES", "3"))

# nonce -> {sender, expires, uses}
_TICKETS: dict[str, dict] = {}

ACCEPT = "audio/*,image/*,application/pdf,text/plain"


def _prune() -> None:
    now = time.time()
    for nonce in [n for n, t in _TICKETS.items() if t["expires"] < now]:
        _TICKETS.pop(nonce, None)


def new_ticket(sender: str) -> str:
    _prune()
    nonce = secrets.token_urlsafe(32)
    _TICKETS[nonce] = {
        "sender": sender,
        "expires": time.time() + TTL_SECONDS,
        "uses": 0,
    }
    return nonce


def _claim(nonce: str) -> dict | None:
    _prune()
    ticket = _TICKETS.get(nonce)
    if not ticket:
        return None
    if ticket["uses"] >= MAX_USES:
        _TICKETS.pop(nonce, None)
        return None
    return ticket


def link_for(sender: str) -> str:
    """Chat-ready text inviting the student to upload. Deterministic: no model."""
    base = (PUBLIC_BASE_URL or "").rstrip("/")
    if not base:
        return (
            "Uploads aren't configured on this deployment yet - type your "
            "question instead."
        )
    nonce = new_ticket(sender)
    minutes = max(1, TTL_SECONDS // 60)
    return (
        "Tap here to send me a *voice note, photo or PDF*:\n"
        f"{base}/upload/{nonce}\n\n"
        "Record yourself asking, photograph a question or a slide, or pick a "
        f"file. I'll read it and answer. The link is yours only and expires in "
        f"{minutes} minutes.\n\n"
        "The answer appears on that page and comes back here too - send: more"
    )


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

_STYLE = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0b0d0c; color:#e8ece9;
    font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  main { max-width:520px; margin:0 auto; padding:40px 22px 80px; }
  h1 { font-size:23px; margin:0 0 10px; font-weight:650; letter-spacing:-.01em; }
  p { color:#8b958f; margin:0 0 20px; font-size:15px; }
  label { display:block; font-size:13px; text-transform:uppercase;
    letter-spacing:.08em; color:#8b958f; margin:0 0 8px; }
  input[type=file], textarea {
    width:100%; background:#121514; color:#e8ece9; border:1px solid #1f2422;
    border-radius:9px; padding:14px; font:inherit; font-size:15px; }
  textarea { min-height:78px; resize:vertical; }
  .field { margin:0 0 20px; }
  button { width:100%; background:#25d366; color:#07130c; border:0;
    border-radius:9px; padding:15px; font:inherit; font-weight:650;
    font-size:16px; cursor:pointer; }
  .note { font-size:13px; color:#8b958f; margin-top:18px; }
  .answer { background:#121514; border:1px solid #1f2422; border-radius:11px;
    padding:18px; white-space:pre-wrap; font-size:15px; margin:0 0 20px; }
  .heard { color:#25d366; font-size:14px; margin:0 0 14px; }
  .bad { color:#ff9d8a; }
  a { color:#25d366; }
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        status_code=status,
        content=(
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
            f"<body><main>{body}</main></body></html>"
        ),
    )


def _expired() -> HTMLResponse:
    return _page(
        "Link expired",
        "<h1>This link has expired</h1>"
        "<p>Send <b>voice</b> in the chat and I'll give you a fresh one.</p>",
        status=410,
    )


@router.get("/upload/{nonce}")
async def upload_form(nonce: str) -> HTMLResponse:
    if not _claim(nonce):
        return _expired()
    return _page(
        "Send a voice note or photo",
        "<h1>Ask out loud, or show me</h1>"
        "<p>Record yourself asking a question, photograph a question or a "
        "slide, or pick a PDF. I'll read it and answer here.</p>"
        '<form method=post enctype="multipart/form-data">'
        '<div class=field><label for=f>Voice note, photo or file</label>'
        f'<input id=f type=file name=file accept="{ACCEPT}" required></div>'
        '<div class=field><label for=q>Anything to add (optional)</label>'
        '<textarea id=q name=note '
        'placeholder="e.g. this is question 3, I don\'t get part b"></textarea>'
        "</div>"
        "<button type=submit>Send it</button>"
        "</form>"
        '<p class=note>Read once to answer you, never saved. Your password is '
        "not involved here.</p>",
    )


@router.post("/upload/{nonce}")
async def upload_submit(
    nonce: str,
    file: UploadFile = File(...),
    note: str = Form(default=""),
) -> HTMLResponse:
    ticket = _claim(nonce)
    if not ticket:
        return _expired()

    sender = ticket["sender"]
    ticket["uses"] += 1

    data = await file.read()
    content_type = file.content_type or ""
    logger.info(
        "upload from %s: %d bytes, content-type %r", sender, len(data), content_type
    )

    result = await asyncio.to_thread(intake.read_bytes, data, content_type)
    status = result.get("status")

    if status != "success":
        reason = {
            "unsupported": "I can read voice notes, photos, and PDF or text files.",
            "empty": "I couldn't make out anything in that.",
        }.get(status, "Something went wrong reading that.")
        logger.warning("upload unreadable: %s", result.get("error_message"))
        return _page(
            "Couldn't read that",
            f"<h1>Couldn't read that</h1><p class=bad>{escape(reason)}</p>"
            "<p>Go back and try again, or just type your question in the chat.</p>",
        )

    kind = result.get("kind") or "document"
    extracted = result.get("text", "")
    question, prefix = intake.question_for(kind, extracted, note)

    try:
        answer = await run_turn(sender, question)
    except Exception as exc:
        logger.exception("upload turn failed")
        outbox.stash_labelled(
            sender, "what you sent", f"Something broke reading that: {type(exc).__name__}"
        )
        return _page(
            "Something broke",
            "<h1>Something broke</h1>"
            "<p class=bad>I read it but couldn't answer. Try again in the "
            "chat.</p>",
        )

    # The student is looking at this page, but the conversation is in WhatsApp.
    # Put the answer in both places.
    outbox.stash_labelled(sender, intake.LABELS.get(kind, "what you sent"), answer)

    heard = (
        f'<p class=heard>Heard: "{escape(intake.shorten(extracted, 160))}"</p>'
        if kind == "voice"
        else ""
    )
    return _page(
        "Answer",
        "<h1>Here you go</h1>"
        f"{heard}"
        f"<div class=answer>{escape(prefix + answer)}</div>"
        "<p>This is waiting in WhatsApp too - send <b>more</b> there.</p>"
        "<p class=note>Send another? Reuse this link while it lasts.</p>",
    )

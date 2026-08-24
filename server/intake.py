"""Read media as a question: voice notes, photos, documents.

WhatsApp's native inputs are the microphone and the camera. A student at 11pm
does not type a well-formed English sentence - they hold the mic button, or they
photograph the question. So media is handed to Gemini directly:

    voice note  -> transcript, used as the student's question
    photo       -> text and diagrams read out of the image
    document    -> the file's contents, read and explained

Where the bytes come from
-------------------------
Not from Twilio, on this account. Fetching MediaUrl0 answers:

    {"code":20003,"message":"This feature is not available on a Trial account"}

while the identical credentials read Messages.json fine and the secret's digest
matches the console token exactly - so the earlier 401 was never a credential
bug, and no code change can fix it. Inbound media therefore arrives through
server/upload.py instead, where the student uploads straight to this service and
Twilio is not involved.

read_media() is kept for a Twilio URL in case the account is ever upgraded, and
is off by default. read_bytes() is the path that actually runs.

Bytes are read in memory. Nothing is written to disk and no media URL is passed
to the model, so a student's photo does not outlive the request.

The transcript is always echoed back. Speech recognition is fallible, and a
wrong answer to a misheard question is far more confusing than a visible "this
is what I heard".
"""

from __future__ import annotations

import hashlib
import logging
import os
from urllib.parse import urljoin

import httpx

from browser_agent.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    VISION_MODEL,
)

logger = logging.getLogger(__name__)

MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_BYTES", str(12 * 1024 * 1024)))
HTTP_TIMEOUT = float(os.getenv("MEDIA_HTTP_TIMEOUT", "30"))
MAX_REDIRECTS = 4

# Twilio media retrieval is a paid-account feature. Left off so the code does
# not repeatedly walk into a 20003; flip it if the account is upgraded.
TWILIO_MEDIA_FETCH = os.getenv("TWILIO_MEDIA_FETCH", "0").strip() in ("1", "true", "True")

_DOCUMENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/csv",
}

# Browsers hand back webm or mp4 from a phone's mic; WhatsApp used ogg. All are
# audio as far as the model is concerned.
_VOICE_TYPES = {"video/3gpp", "video/webm", "video/mp4", "video/quicktime"}

LABELS = {
    "voice": "your voice note",
    "image": "your photo",
    "document": "your file",
}

_TRANSCRIBE_PROMPT = (
    "Transcribe this recording exactly as spoken. It is a university student "
    "asking their study assistant a question, often in English mixed with "
    "Kiswahili or local slang, sometimes with background noise.\n\n"
    "Rules:\n"
    "- Return ONLY the transcription. No preamble, no quotes, no commentary.\n"
    "- Keep the student's own wording and language. Do not translate.\n"
    "- Keep unit codes, module names and numbers exactly as spoken.\n"
    "- If the audio is silent or unintelligible, return exactly: NO_SPEECH"
)

_IMAGE_PROMPT = (
    "A university student photographed this and sent it to their study "
    "assistant. Read it for the assistant.\n\n"
    "Rules:\n"
    "- Transcribe ALL visible text verbatim, including question numbers, marks "
    "in brackets, dates and deadlines.\n"
    "- Describe any diagram, table, graph or code block in enough detail to be "
    "reasoned about, after the text.\n"
    "- Do not answer the question and do not add advice. Only report what is "
    "there.\n"
    "- If the image has no readable content, return exactly: NOT_READABLE"
)

_DOCUMENT_PROMPT = (
    "A university student sent this file to their study assistant. Extract its "
    "contents for the assistant.\n\n"
    "Rules:\n"
    "- Report the text faithfully, keeping question numbering, marks, "
    "deadlines and required formats exactly as written.\n"
    "- Summarise long passages, but never invent or complete missing parts.\n"
    "- Do not answer any questions in the file. Only report what is there.\n"
    "- If nothing can be read, return exactly: NOT_READABLE"
)

_PROMPTS = {
    "voice": _TRANSCRIBE_PROMPT,
    "image": _IMAGE_PROMPT,
    "document": _DOCUMENT_PROMPT,
}


def shorten(text: str, limit: int = 140) -> str:
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "\u2026"


def kind_of(content_type: str) -> str | None:
    """Maps a MIME type to one of: voice, image, document. None if unsupported."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime.startswith("audio/") or mime in _VOICE_TYPES:
        return "voice"
    if mime.startswith("image/"):
        return "image"
    if mime in _DOCUMENT_TYPES:
        return "document"
    return None


def question_for(kind: str, extracted: str, note: str = "") -> tuple[str, str]:
    """Turns extracted media into (question for the agent, prefix for the reply).

    Shared by the webhook and the upload page on purpose: these two paths
    answering the same voice note differently would be a bug nobody would spot
    until a demo.
    """
    extracted = (extracted or "").strip()
    note = (note or "").strip()

    if kind == "voice":
        # The transcript IS the question. Echo it, because answering a misheard
        # question without showing what was heard is deeply confusing.
        question = f"{extracted}\n\n{note}".strip() if note else extracted
        return question, f'\U0001f399 *Heard:* "{shorten(extracted)}"\n\n'

    noun = "photo" if kind == "image" else "file"
    question = (
        f"The student sent a {noun}. This is what it contains:\n\n"
        f"{extracted}\n\n"
        + (f"Their message with it: {note}\n\n" if note else "")
        + "Answer them about it, grounded in their own unit material where that "
        "is relevant. If it is coursework, explain how to approach it and what "
        "the question is really asking - never write the submission for them."
    )
    return question, ""


def _credential_fingerprint() -> str:
    """Identifies the credentials in memory without disclosing them."""
    sid = TWILIO_ACCOUNT_SID
    token = TWILIO_AUTH_TOKEN
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8] if token else "-"
    return (
        f"sid_prefix={sid[:2]!r} sid_len={len(sid)} sid_tail={sid[-4:]!r} "
        f"token_len={len(token)} token_sha8={digest}"
    )


def _fetch(url: str) -> tuple[bytes, str]:
    """Downloads Twilio-hosted media, authenticating the first hop only.

    api.twilio.com needs the account credentials and replies 307 to a
    pre-signed CDN URL. That signed URL must be requested WITHOUT the
    Authorization header, or it is refused as a double authorisation.
    """
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        response = client.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        logger.info("media hop 0: HTTP %s", response.status_code)

        if response.status_code in (401, 403):
            logger.error("media auth rejected: %s", _credential_fingerprint())

        hops = 0
        while response.is_redirect and hops < MAX_REDIRECTS:
            location = response.headers.get("location", "")
            if not location:
                break
            target = urljoin(str(response.url), location)
            response = client.get(target)  # signed URL: no auth
            hops += 1
            logger.info("media hop %d: HTTP %s", hops, response.status_code)

        if response.status_code >= 400:
            raise RuntimeError(f"media fetch returned HTTP {response.status_code}")

        return response.content, response.headers.get("content-type", "")


def read_bytes(data: bytes, content_type: str) -> dict:
    """Reads media we already hold, with Gemini.

    Blocking on purpose: called with asyncio.to_thread so one student's upload
    cannot stall everyone else's turns.

    Returns status "success" with `kind` and `text`, or "unsupported" / "empty"
    / "error" with an error_message.
    """
    kind = kind_of(content_type)
    if kind is None:
        logger.warning("unsupported media type %r", content_type)
        return {
            "status": "unsupported",
            "error_message": f"unsupported media type: {content_type!r}",
        }

    if not data:
        return {"status": "empty", "kind": kind, "error_message": "empty file"}
    if len(data) > MAX_MEDIA_BYTES:
        return {
            "status": "error",
            "kind": kind,
            "error_message": (
                f"that file is {len(data) // (1024 * 1024)}MB, which is larger "
                "than I can read"
            ),
        }

    mime = (content_type or "").split(";")[0].strip().lower()

    try:
        from google import genai
        from google.genai import types

        client = genai.Client()
        response = client.models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Part.from_bytes(data=data, mime_type=mime),
                _PROMPTS[kind],
            ],
        )
        text = (response.text or "").strip()
    except Exception as exc:
        logger.exception("gemini could not read %s (%s)", kind, mime)
        return {"status": "error", "kind": kind, "error_message": str(exc)}

    if not text or text in {"NO_SPEECH", "NOT_READABLE"}:
        logger.info("%s had nothing readable", kind)
        return {
            "status": "empty",
            "kind": kind,
            "error_message": "nothing readable in the media",
        }

    logger.info("read %s: %d bytes, %s, %d chars", kind, len(data), mime, len(text))
    return {"status": "success", "kind": kind, "text": text, "bytes": len(data)}


def read_media(url: str, content_type: str) -> dict:
    """Reads media hosted by Twilio. Requires a paid account (see module docs)."""
    if not TWILIO_MEDIA_FETCH:
        return {
            "status": "error",
            "kind": kind_of(content_type),
            "error_message": "twilio media retrieval is disabled (trial account)",
        }
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {
            "status": "error",
            "kind": kind_of(content_type),
            "error_message": "twilio credentials are not configured",
        }
    try:
        data, served_type = _fetch(url)
    except Exception as exc:
        logger.exception("failed to download inbound media")
        return {"status": "error", "kind": kind_of(content_type), "error_message": str(exc)}

    served = (served_type or "").split(";")[0].strip().lower()
    # Ignore a generic octet-stream: the model needs a real media type.
    mime = served if served and served != "application/octet-stream" else content_type
    return read_bytes(data, mime)

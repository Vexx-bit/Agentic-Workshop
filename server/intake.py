"""Read WhatsApp media as a question: voice notes, photos, documents.

WhatsApp's native inputs are the microphone and the camera. A student at 11pm
does not type a well-formed English sentence - they hold the mic button, or they
photograph the question. Ignoring that would waste both the most WhatsApp-native
thing about the product and the most Gemini-native thing about the model.

So inbound media is handed to Gemini directly:

    voice note  -> transcript, used as the student's question
    photo       -> text and diagrams read out of the image
    document    -> the file's contents, read and explained

Fetching that media is the fiddly part, and it is worth spelling out because it
silently broke once. Twilio's MediaUrl0 lives on api.twilio.com and requires the
account credentials as HTTP basic auth. It then answers 307 with a Location
pointing at a PRE-SIGNED CDN URL. That signed URL must be fetched WITHOUT
credentials: the signature is already the authorisation, and sending an
Authorization header alongside it is two auth mechanisms for one request, which
is rejected. An HTTP client set to follow redirects automatically carries the
header across and fails. So the hops are walked by hand below.

Bytes go to the model in memory. Nothing is written to disk and no media URL is
passed to the model, so a student's photo does not outlive the request.

The transcript is always echoed back to the student. Speech recognition is
fallible, and a wrong answer to a misheard question is far more confusing than a
visible "this is what I heard".
"""

from __future__ import annotations

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

# WhatsApp caps media around 16MB; stay under it and fail politely if not.
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_BYTES", str(12 * 1024 * 1024)))
HTTP_TIMEOUT = float(os.getenv("MEDIA_HTTP_TIMEOUT", "30"))
MAX_REDIRECTS = 4

_DOCUMENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/csv",
}

_TRANSCRIBE_PROMPT = (
    "Transcribe this voice note exactly as spoken. It is a university student "
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


def kind_of(content_type: str) -> str | None:
    """Maps a MIME type to one of: voice, image, document. None if unsupported."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime.startswith("audio/") or mime == "video/3gpp":
        # WhatsApp voice notes arrive as audio/ogg; some Android builds send
        # a 3gpp container for the same mic button.
        return "voice"
    if mime.startswith("image/"):
        return "image"
    if mime in _DOCUMENT_TYPES:
        return "document"
    return None


def _fetch(url: str) -> tuple[bytes, str]:
    """Downloads Twilio-hosted media, authenticating the first hop only.

    api.twilio.com needs the account credentials and replies 307 to a
    pre-signed CDN URL. That signed URL must be requested WITHOUT the
    Authorization header, or it is refused as a double authorisation.
    """
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        response = client.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        logger.info("media hop 0: HTTP %s", response.status_code)

        hops = 0
        while response.is_redirect and hops < MAX_REDIRECTS:
            location = response.headers.get("location", "")
            if not location:
                break
            target = urljoin(str(response.url), location)
            # No auth from here on: the URL carries its own signature.
            response = client.get(target)
            hops += 1
            logger.info("media hop %d: HTTP %s", hops, response.status_code)

        if response.status_code >= 400:
            raise RuntimeError(f"media fetch returned HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "")
        logger.info(
            "media fetched: %d bytes, content-type %r", len(response.content), content_type
        )
        return response.content, content_type


def read_media(url: str, content_type: str) -> dict:
    """Reads one inbound media item with Gemini.

    Blocking on purpose: called from the event loop with asyncio.to_thread, so
    the HTTP fetch and the model call cannot stall other students' turns.

    Returns a dict with status "success" plus `kind` and `text`, or status
    "unsupported" / "empty" / "error" with an error_message.
    """
    kind = kind_of(content_type)
    if kind is None:
        logger.warning("unsupported inbound media type %r", content_type)
        return {
            "status": "unsupported",
            "error_message": f"unsupported media type: {content_type!r}",
        }

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return {
            "status": "error",
            "kind": kind,
            "error_message": "twilio credentials are not configured",
        }

    try:
        data, served_type = _fetch(url)
    except Exception as exc:
        logger.exception("failed to download inbound media")
        return {"status": "error", "kind": kind, "error_message": str(exc)}

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

    # Prefer the type Twilio actually served, but ignore a generic
    # octet-stream: the model needs a real media type to decode.
    served = (served_type or "").split(";")[0].strip().lower()
    announced = (content_type or "").split(";")[0].strip().lower()
    mime = served if served and served != "application/octet-stream" else announced

    prompt = {
        "voice": _TRANSCRIBE_PROMPT,
        "image": _IMAGE_PROMPT,
        "document": _DOCUMENT_PROMPT,
    }[kind]

    try:
        from google import genai
        from google.genai import types

        client = genai.Client()
        response = client.models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Part.from_bytes(data=data, mime_type=mime),
                prompt,
            ],
        )
        text = (response.text or "").strip()
    except Exception as exc:
        logger.exception("gemini could not read inbound %s (%s)", kind, mime)
        return {"status": "error", "kind": kind, "error_message": str(exc)}

    if not text or text in {"NO_SPEECH", "NOT_READABLE"}:
        logger.info("inbound %s had nothing readable", kind)
        return {
            "status": "empty",
            "kind": kind,
            "error_message": "nothing readable in the media",
        }

    logger.info("read inbound %s: %d bytes, %s, %d chars", kind, len(data), mime, len(text))
    return {"status": "success", "kind": kind, "text": text, "bytes": len(data)}

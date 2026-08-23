"""FastAPI ingress for the WhatsApp-operated browser agent (Twilio).

Key constraint: Twilio's webhook expects a response within ~10-15s, and browser
automation is far slower than that. So we ACK immediately with empty TwiML and
do the real work in a background task, replying via the Twilio REST API.

Run locally:
    uvicorn server.main:app --reload --port 8000
    ngrok http 8000
Then point the Twilio WhatsApp sandbox "WHEN A MESSAGE COMES IN" webhook at:
    https://<your-ngrok-domain>/whatsapp    (HTTP POST)
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, Form, Request, Response

from browser_agent.config import (
    PUBLIC_BASE_URL,
    TWILIO_AUTH_TOKEN,
    TWILIO_VALIDATE_SIGNATURE,
)
from server import whatsapp
from server.runner import run_turn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whatsapp-browser-agent")

app = FastAPI(title="WhatsApp Browser Agent")

from server.media import router as media_router
app.include_router(media_router)

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

HELP_TEXT = (
    "Browser agent ready.\n\n"
    "Try:\n"
    "- open saucedemo and tell me the first 3 product prices\n"
    "- log in as standard_user and list what's in the cart\n"
    "- go to the-internet.herokuapp.com and find the login form\n\n"
    "Anything that changes a site (submit, buy, delete) I will ask you to "
    "confirm with YES first."
)


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


async def _handle(sender: str, body: str) -> None:
    """Background worker: run the agent, then reply over WhatsApp."""
    try:
        reply = await run_turn(sender, body)
    except Exception as exc:  # never leave the user hanging
        logger.exception("agent turn failed")
        reply = (
            "Something broke while driving the browser: "
            f"{type(exc).__name__}. Try again, or send 'reset' style wording "
            "with a simpler request."
        )

    try:
        whatsapp.send(sender, reply)
    except Exception:
        logger.exception("failed to send whatsapp reply to %s", sender)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


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

    if body.lower() in {"hi", "hello", "help", "start", "/start"}:
        background.add_task(whatsapp.send, sender, HELP_TEXT)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    if not body:
        background.add_task(
            whatsapp.send, sender, "Send me a text instruction (media isn't supported yet)."
        )
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    # Fast ack + async work: this is the non-negotiable timeout rule.
    background.add_task(whatsapp.send, sender, "On it — opening the browser…")
    background.add_task(_handle, sender, body)
    return Response(content=EMPTY_TWIML, media_type="application/xml")

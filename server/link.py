"""Single-use HTTPS page where a student links their own Moodle account.

How the password is handled, stated plainly
-------------------------------------------
- It arrives once, over HTTPS, in a form POST to this service.
- It is exchanged for a Moodle web-service token inside one function call and
  then goes out of scope. It is never stored, never logged, never echoed back
  into the page.
- It never travels through WhatsApp. Chat messages sit on the phone and in
  Twilio's logs, so a password typed into chat is a password leaked.

What is kept is the token, under an opaque key (a salted hash of the phone
number). A token carries only that student's own permissions, and the student
can revoke it at any time by sending "unlink".

Why a nonce and not a login form
--------------------------------
The page is reachable only through a single-use id that the student was sent in
their own WhatsApp thread, valid for ten minutes and capped at five attempts.
There is no username enumeration surface and nothing to brute-force after the
window closes.
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from browser_agent import config, moodle, store

logger = logging.getLogger(__name__)

router = APIRouter()

# Never cached, never indexed.
_HEADERS = {"Cache-Control": "no-store, max-age=0", "X-Robots-Tag": "noindex, nofollow"}

_STYLE = (
    "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
    "margin:0;padding:24px;background:#f6f7f9;color:#111}"
    ".card{max-width:420px;margin:0 auto;background:#fff;border-radius:14px;"
    "padding:22px;box-shadow:0 1px 3px rgba(0,0,0,.12)}"
    "h1{font-size:20px;margin:0 0 6px}p{font-size:14px;line-height:1.5;color:#444}"
    "label{display:block;font-size:13px;font-weight:600;margin:14px 0 6px}"
    "input{width:100%;box-sizing:border-box;padding:11px;font-size:16px;"
    "border:1px solid #ccd0d5;border-radius:9px}"
    "button{width:100%;margin-top:18px;padding:13px;font-size:16px;font-weight:600;"
    "color:#fff;background:#128c7e;border:0;border-radius:9px}"
    ".err{background:#fdecea;color:#8b1a10;padding:10px;border-radius:8px;font-size:13px}"
    ".ok{background:#e7f6ec;color:#0f5132;padding:10px;border-radius:8px;font-size:14px}"
    ".note{font-size:12px;color:#667}"
)


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        status_code=status_code,
        headers=_HEADERS,
        content=(
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<meta name=robots content='noindex,nofollow'>"
            f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
            f"<body><div class=card>{body}</div></body></html>"
        ),
    )


def _site_label() -> str:
    """Shows the student which site they are about to sign in to."""
    base = (config.MOODLE_BASE_URL or "").split("://")[-1].strip("/")
    return html.escape(base or "your university e-learning site")


def _form_page(nonce: str, error: str = "") -> HTMLResponse:
    banner = f"<p class=err>{html.escape(error)}</p>" if error else ""
    minutes = max(1, store.LINK_TTL_SECONDS // 60)
    return _page(
        "Link your Moodle account",
        "<h1>Link your Moodle account</h1>"
        f"<p>Sign in to <b>{_site_label()}</b> so the WhatsApp assistant can see "
        "your own units, notes and deadlines.</p>"
        f"{banner}"
        f"<form method=post action='/link/{html.escape(nonce)}' autocomplete=off>"
        "<label for=username>Moodle username</label>"
        "<input id=username name=username inputmode=text autocapitalize=none "
        "autocorrect=off required>"
        "<label for=password>Moodle password</label>"
        "<input id=password name=password type=password required>"
        "<button type=submit>Link my account</button></form>"
        "<p class=note>Your password is swapped for an access token the moment "
        "you press the button, and is not saved or logged. This page works once "
        f"and expires {minutes} minutes after it was sent. Send <b>unlink</b> on "
        "WhatsApp at any time to delete the token.</p>",
    )


def _expired_page() -> HTMLResponse:
    return _page(
        "Link expired",
        "<h1>This link has expired</h1>"
        "<p>Links work once and only for a few minutes. Go back to WhatsApp and "
        "send <b>link</b> to get a fresh one.</p>",
        status_code=410,
    )


@router.get("/link/{nonce}")
async def link_form(nonce: str) -> HTMLResponse:
    if not store.read_nonce(nonce):
        return _expired_page()
    return _form_page(nonce)


@router.post("/link/{nonce}")
async def link_submit(
    nonce: str,
    username: str = Form(default=""),
    password: str = Form(default=""),
) -> HTMLResponse:
    record = store.read_nonce(nonce)
    if not record:
        return _expired_page()

    if not username.strip() or not password:
        return _form_page(nonce, "Enter both your username and your password.")

    attempts = store.bump_nonce_attempt(nonce)
    if attempts < 0:
        return _expired_page()
    if attempts > store.MAX_LINK_ATTEMPTS:
        store.consume_nonce(nonce)
        return _page(
            "Too many attempts",
            "<h1>Too many attempts</h1>"
            "<p>This link has been closed. Send <b>link</b> on WhatsApp for a "
            "new one.</p>",
            status_code=429,
        )

    try:
        token = moodle.exchange_password_for_token(username.strip(), password)
    except Exception as exc:
        # Log the failure type only. The message can echo submitted values.
        logger.warning("link attempt failed (%s)", type(exc).__name__)
        remaining = max(0, store.MAX_LINK_ATTEMPTS - attempts)
        return _form_page(
            nonce,
            "Moodle did not accept that username and password. "
            f"{remaining} attempt(s) left on this link.",
        )

    user_key = str(record.get("user_key") or "")
    if not user_key:
        store.consume_nonce(nonce)
        return _expired_page()

    moodle.set_token_for(user_key, token)

    # Prove the token actually works, and greet them by their own name. A
    # "success" page that has not made one real call is not evidence.
    who = ""
    try:
        profile = moodle.whoami(user_key)
        who = str(profile.get("fullname") or "").strip()
        if profile.get("userid"):
            moodle.set_token_for(user_key, token, profile.get("userid"))
    except Exception:
        logger.warning("token stored but verification call failed")

    store.consume_nonce(nonce)
    del token, password

    greeting = f"Linked as <b>{html.escape(who)}</b>." if who else "Your account is linked."
    return _page(
        "Linked",
        "<h1>You are linked</h1>"
        f"<p class=ok>{greeting}</p>"
        "<p>Go back to WhatsApp and try:</p>"
        "<p><b>what are my units</b><br>"
        "<b>what is mobile programming about now</b><br>"
        "<b>send me the assignment questions for mobile programming</b></p>"
        "<p class=note>Your password was not saved. Send <b>unlink</b> on "
        "WhatsApp to delete the stored token.</p>",
    )

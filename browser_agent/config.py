"""Central configuration for the WhatsApp-operated browser agent.

Everything is read from the environment so that local dev uses a plain `.env`
and Cloud Run can use Secret Manager-backed env vars without code changes.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root (parent of this package) if present.
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")

APP_NAME = os.getenv("APP_NAME", "whatsapp_browser_agent")

# --- Models ---
# Kept in the environment on purpose: Google retires model IDs faster than we
# can rebuild an image. gemini-2.5-flash started answering 404 NOT_FOUND
# ("no longer available to new users") mid-build, and swapping this default is
# the entire fix - no code change, no redeploy.
AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.6-flash")
VISION_MODEL = os.getenv("VISION_MODEL", "gemini-3.6-flash")

# --- Browser / artifacts ---
ARTIFACT_DIR = Path(os.getenv("BROWSER_ARTIFACT_DIR", str(REPO_ROOT / "artifacts"))).resolve()
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "1") not in ("0", "false", "False", "")
BROWSER_ALLOWED_ORIGINS = os.getenv("BROWSER_ALLOWED_ORIGINS", "").strip()

# --- Playwright MCP transport ---
# Empty  -> run the MCP server locally as a stdio subprocess (npx).
# Set    -> talk to a remote Playwright MCP service over streamable HTTP.
PLAYWRIGHT_MCP_URL = os.getenv("PLAYWRIGHT_MCP_URL", "").strip()
# Audience for the Cloud Run ID token. Defaults to the service root derived
# from PLAYWRIGHT_MCP_URL, which is what Cloud Run expects. If the browser
# service answers 403, the audience and the URL host have diverged - Cloud Run
# services answer on two hostnames and the token must match the one called.
PLAYWRIGHT_MCP_TOKEN_AUDIENCE = os.getenv("PLAYWRIGHT_MCP_TOKEN_AUDIENCE", "").strip()

# --- Twilio ---
# NOTE: a Twilio trial account cannot send free-form WhatsApp bodies over the
# REST API - Messages.json answers 400 `21654 ContentSid Required` for any
# `body`, on every sender including the sandbox. That restriction does not
# apply to a TwiML response returned from the webhook, which is why
# server/main.py answers WhatsApp inline instead of calling the API. No account
# upgrade needed.
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "0") in ("1", "true", "True")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# WhatsApp hard limit per message body.
WHATSAPP_MAX_CHARS = 1500

# --- Telegram ---
# Free, no message templates, no 24h session window, no per-message cost.
# Token comes from @BotFather. The webhook secret is echoed back by Telegram in
# the X-Telegram-Bot-Api-Secret-Token header, which is how we authenticate
# inbound calls (the Telegram equivalent of Twilio's request signature).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
TELEGRAM_MAX_CHARS = 4000

# --- Moodle (LMS) ---
# Base site URL, no trailing slash, e.g. the university e-learning host.
MOODLE_BASE_URL = os.getenv("MOODLE_BASE_URL", "").strip().rstrip("/")
# Single-user fallback token, for the demo. Per-student tokens supersede it at
# runtime via browser_agent.moodle.set_token_for().
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN", "").strip()
# How long a proxied file link stays alive, in seconds.
MOODLE_MEDIA_TTL_SECONDS = int(os.getenv("MOODLE_MEDIA_TTL_SECONDS", "900"))

# --- Demo target ---
DEMO_SITE_URL = os.getenv("DEMO_SITE_URL", "https://www.saucedemo.com")
DEMO_USERNAME = os.getenv("DEMO_USERNAME", "")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "")

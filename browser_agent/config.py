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
AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-2.5-flash")
VISION_MODEL = os.getenv("VISION_MODEL", "gemini-2.5-flash")

# --- Browser / artifacts ---
ARTIFACT_DIR = Path(os.getenv("BROWSER_ARTIFACT_DIR", str(REPO_ROOT / "artifacts"))).resolve()
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "1") not in ("0", "false", "False", "")
BROWSER_ALLOWED_ORIGINS = os.getenv("BROWSER_ALLOWED_ORIGINS", "").strip()

# --- Twilio ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "0") in ("1", "true", "True")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# WhatsApp hard limit per message body.
WHATSAPP_MAX_CHARS = 1500

# --- Demo target ---
DEMO_SITE_URL = os.getenv("DEMO_SITE_URL", "https://www.saucedemo.com")
DEMO_USERNAME = os.getenv("DEMO_USERNAME", "")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "")

# WhatsApp-Operated Browser Agent

A chat-operated browser automation agent built on Google ADK. You message it on
WhatsApp, it drives a real browser, and it reports back in plain language.

## Architecture

```
WhatsApp user
    |
    v
Twilio WhatsApp sandbox (webhook, HTTP POST)
    |
    v
FastAPI ingress  (server/main.py)
    |  fast ACK (empty TwiML) + background task
    v
ADK agent  (browser_agent/agent.py)
    |
    +--> Playwright MCP        <- PRIMARY: accessibility tree / DOM
    |
    +--> Vision fallback       <- ONLY if the DOM read fails
    |     (browser_take_screenshot -> Gemini vision)
    v
Twilio REST API --> reply back to the user
```

The messaging layer is a swappable adapter. `browser_agent/` knows nothing about
Twilio; swapping in Meta's WhatsApp Cloud API later touches only `server/`.

## What's in this build

| Piece | File | Notes |
|---|---|---|
| Agent + tool wiring | `browser_agent/agent.py` | Playwright MCP over stdio, filtered tool surface |
| DOM-first policy | `browser_agent/agent.py` (instruction) | snapshot/find before any vision call |
| Vision fallback | `browser_agent/vision.py` | explicit separate code path, reads screenshot from disk |
| Confirmation gate | `browser_agent/guardrails.py` | `before_tool_callback`, not just a prompt |
| Per-user sessions | `server/runner.py` | session keyed by hash of the WhatsApp sender |
| Twilio ingress | `server/main.py` | fast ack, async reply, optional signature validation |
| Outbound + chunking | `server/whatsapp.py` | splits replies under WhatsApp limits |
| Deploy | `Dockerfile` | Python + Node + Chromium for Cloud Run |

## Local setup

Prereqs: Python 3.12, Node 18+ (for `npx @playwright/mcp`), a Gemini API key,
and a Twilio account on the free tier.

```bash
# from the repo root
pip install -r requirements.txt
npx playwright install chromium
cp .env.example .env   # then fill in real values
```

### Step 1 — test the agent alone (no messaging platform)

Always run ADK from the **parent** directory of the agent folder:

```bash
adk web            # then pick "browser_agent" in the dropdown
# or
adk run browser_agent
```

Try: `open https://www.saucedemo.com and list the first three product prices`.

Then force the fallback branch: ask about something that isn't in the
accessibility tree (a canvas-rendered chart, an image-only label) and confirm
the agent takes a screenshot and calls `read_screenshot_with_vision`.

### Step 2 — wire up WhatsApp (Twilio sandbox)

```bash
uvicorn server.main:app --reload --port 8000
ngrok http 8000
```

In the Twilio Console: **Messaging -> Try it out -> Send a WhatsApp message**.

1. Join the sandbox from your phone (`join <two-words>` to `+1 415 523 8886`).
2. Set **WHEN A MESSAGE COMES IN** to `https://<ngrok-domain>/whatsapp`,
   method `HTTP POST`.
3. Message the sandbox number. You should get an instant "On it" ack, then the
   real answer once the browser run finishes.

Set `TWILIO_VALIDATE_SIGNATURE=1` and `PUBLIC_BASE_URL=https://<domain>` once
the URL is stable.

## Human-in-the-loop behaviour

Reading the web is unrestricted. These are blocked until you reply YES:

- always: file upload, JS evaluation, unsafe Playwright code, dialogs, drops
- when the target looks side-effecting: clicks/typing matching keywords like
  `submit`, `buy`, `checkout`, `pay`, `delete`, `send`, `register`

The gate is enforced in code (`before_tool_callback`), and an approval is
single-use and bound to the exact tool arguments that were shown to you. A
second identical action asks again.

## Demo plan

1. Primary target: `https://www.saucedemo.com` and
   `https://the-internet.herokuapp.com` — no real data, safe on a projector.
2. Show the DOM-first read, then a deliberate vision-fallback case.
3. Show the confirmation gate blocking a checkout, then approving it.
4. Real institutional site (LMS) as a secondary "it also works" moment, with a
   pre-recorded clip as backup.

## Secrets

`.env` is gitignored, and so are `artifacts/` and screenshots. For Cloud Run,
move `GOOGLE_API_KEY`, `TWILIO_AUTH_TOKEN` and any site credentials into Secret
Manager and reference them as env vars — do not bake them into the image.

## Known gaps / next steps

- Single shared browser context, serialised by a lock. Fine for a demo, not for
  concurrent users.
- `InMemorySessionService` means sessions reset on restart. Swap for a
  persistent session service if you deploy for real.
- The repo currently has `.venv/` committed on `main`; it should be removed from
  git history before the repo is shown off.
- Phase 2 (stretch): Meta WhatsApp Cloud API as a second adapter under
  `server/`, leaving `browser_agent/` untouched.

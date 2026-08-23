# Study Agent on WhatsApp

A chat-operated agent that gives students their university e-learning account
over WhatsApp: ask what a topic is about, get the assignment questions, pull
down this week's slides, tick off an activity, set a reminder.

Built for the Agentic AI Workshop hackathon on **Google ADK + Gemini**, running
on **Cloud Run**, reachable through **Twilio WhatsApp**.

> It fetches and explains coursework. It does not do it, and it cannot submit
> it. That boundary is enforced in code, not in a prompt.

---

## What a student can ask

| Message | What happens |
|---|---|
| `link` | Single-use HTTPS page; sign in once, password never touches the chat |
| *what are my units* | Their real enrolment list |
| *what is mobile programming about now* | Reads the lecturer's latest slides and explains them |
| *explain recyclerview from our slides* | Downloads that deck, extracts the text, answers from it |
| *send me the assignment questions* | Questions, deadline, accepted file types, size cap, brief documents |
| *what have I not finished in software testing* | Outstanding activities, split by who can complete them |
| *how am I doing* | Completion percentage per unit, weakest first |
| *remind me Friday 6pm to finish the lab* | Writes a reminder into their own calendar (asks first) |
| `unlink` | Token deleted immediately |

---

## Architecture

```
WhatsApp user
  |
  v
Twilio webhook  ->  FastAPI ingress            (server/main.py)
                      |  fast ACK, work continues in the background
                      v
                    ADK agent                  (browser_agent/agent.py)
                      |
         +------------+-------------------------------+
         |                                            |
         v                                            v
   Moodle REST tools                          Playwright MCP
   per-student token                          DOM / accessibility tree first
   (browser_agent/moodle.py, study.py)        vision fallback on a DOM miss
         |                                            |
         +------------+-------------------------------+
                      v
                 Twilio REST reply             (server/whatsapp.py)
```

**Two paths, chosen explicitly.** The e-learning site is *always* reached over
its web-service API and never with the browser: it permits one interactive
session per user, so a headless login there could log a student out of their own
laptop mid-class. API calls do not consume that session, which is also why many
students can use the bot simultaneously.

**The messaging platform is an adapter.** `browser_agent/` knows nothing about
Twilio. Swapping to Meta's WhatsApp Cloud API or adding Telegram touches
`server/` only.

---

## Project structure

```
.
|-- browser_agent/            the agent itself (no knowledge of WhatsApp)
|   |-- agent.py              root_agent: model, instruction, tool registry
|   |-- config.py             every env var, read in one place
|   |-- guardrails.py         before_tool_callback: state-changing tools stop
|   |                         and ask for YES, bound to exact arguments
|   |-- mcp_transport.py      Playwright MCP wiring, stdio locally / HTTP on
|   |                         Cloud Run, with a filtered tool surface
|   |-- moodle.py             REST layer: allowlist, denylist, per-student
|   |                         tokens, link/unlink, deadlines, briefs, notes
|   |-- study.py              progress, what's left, and reading the
|   |                         lecturer's real pptx/docx/pdf material
|   |-- store.py              shared store for tokens, link nonces and media
|   |                         (memory or Firestore, chosen by env var)
|   `-- vision.py             screenshot -> Gemini, the explicit fallback path
|
|-- server/                   the interface layer (swappable)
|   |-- main.py               Twilio webhook, deterministic link/unlink,
|   |                         help text, /healthz
|   |-- link.py               the single-use HTTPS sign-in page
|   |-- media.py              expiring /media/<id> proxy so the API token
|   |                         never leaves this service
|   |-- runner.py             ADK sessions per sender, per-sender locks,
|   |                         concurrency cap
|   `-- whatsapp.py           outbound send + message chunking
|
|-- web/                      static landing page, deploy on Vercel with
|                             Root Directory = web
|-- deploy/                   deploy.sh (bash) and deploy.ps1 (PowerShell)
|-- docs/                     the long-form documentation, see below
|-- Dockerfile                Playwright base image + Python 3.12
|-- cloudbuild.yaml           image build for Cloud Run
|-- requirements.txt
|-- .env.example              copy to .env for local runs
|
|-- my_agent/                 original workshop agent, kept as reference
`-- multi_tool_agent/         original multi-tool pattern, kept as reference
```

The two workshop folders are deliberately untouched, so the repo still reads as
the skeleton it grew from.

---

## Workflow

### 1. Local, agent only (free, fastest loop)

Always run ADK from the **repo root**, never from inside an agent folder.

```bash
pip install -r requirements.txt
npx playwright install chromium
cp .env.example .env        # then fill in your keys
adk web                     # pick browser_agent in the dev UI
```

This is where tool logic should be tested. It costs nothing and shows the full
trace.

### 2. Local, full WhatsApp path

```bash
uvicorn server.main:app --port 8000
ngrok http 8000
```

Point the Twilio sandbox at `<public-url>/whatsapp` under
**Messaging > Try it out > Send a WhatsApp message > Sandbox settings**.

### 3. Deploy

```powershell
.\deploy\deploy.ps1       # Windows
```

```bash
./deploy/deploy.sh        # bash
```

Deploys two Cloud Run services: the public agent, and a private Playwright
browser service the agent is granted permission to call. Full walkthrough and
the IAM roles are in `docs/DEPLOY_CLOUD_RUN.md`.

### 4. Landing page

Vercel > New Project > this repo > **Root Directory = `web`** > preset *Other*.
The directory only appears once the branch is merged into the default branch.

---

## Request lifecycle

1. Twilio POSTs the message; the webhook validates the signature and **acks
   immediately** with empty TwiML. A browser turn takes longer than Twilio will
   wait, so the reply is sent afterwards over the REST API.
2. `link` and `unlink` are handled deterministically, with no model involved -
   linking is the one step a confused model must not be able to break.
3. The turn runs against an ADK session keyed by a salted hash of the sender.
   Per-sender locks keep one student's messages ordered; a semaphore caps total
   in-flight turns so a class cannot exhaust the container.
4. The agent picks a path: REST for coursework, browser for everything else.
5. State-changing tools return `confirmation_required`. The agent describes the
   action, waits for a plain YES, then retries the identical call.
6. The reply is chunked to WhatsApp's limits and sent.

---

## Safety model

| Layer | What it does |
|---|---|
| Allowlist | Only named API functions can be called at all |
| Denylist | Submission, quiz and grade functions are blocked independently, so widening the allowlist cannot accidentally unblock them |
| Confirmation gate | Anything state-changing stops and asks, bound to the exact arguments shown |
| Per-student tokens | A student's token carries only their own permissions |
| Salted keys | Store keys are hashes of the phone number, so a dump is not a contact list |
| Untrusted content | Course material is data, never instructions; a prompt hidden in a slide deck cannot redirect the agent |
| No credentials in chat | Passwords are refused in messages and only accepted on the one-time HTTPS page |

---

## Documentation

| Document | Contents |
|---|---|
| `docs/WHATSAPP_BROWSER_AGENT.md` | Setup, demo script, known gaps |
| `docs/MULTI_USER_LINKING.md` | Link flow, threat model, memory vs Firestore |
| `docs/DEPLOY_CLOUD_RUN.md` | Cloud Run walkthrough, IAM, secrets |
| `web/README.md` | Landing page deploy and configuration |

---

## Known limitations

Stated plainly, because a demo that hides these is worse than one that doesn't:

- **One shared browser.** Two simultaneous *browser* turns can interleave in the
  same tab. The coursework path never touches the browser.
- **`TOKEN_STORE=memory` is single-instance.** Deploy pinned to one instance, or
  switch to `firestore` for real scale-out.
- **Chat history is in memory.** Tokens survive a restart on Firestore;
  conversation context does not.
- **The MCP identity token is fetched once** at toolset construction, so sessions
  longer than an hour would need a per-request provider.
- **Twilio sandbox**, not a production WhatsApp sender: each new device sends the
  join phrase once, and rate limits are real.

---

## Credits

Google ADK, Gemini, Playwright MCP, FastAPI, Twilio, Cloud Run.
Not affiliated with any university.

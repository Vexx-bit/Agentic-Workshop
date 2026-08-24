# Study Agent on WhatsApp

A chat-operated study agent that gives students their university e-learning
account inside WhatsApp. Ask what a topic is about, get the real assignment
questions, pull down this week's slides, get quizzed on them, tick off an
activity, plan your day.

<video src="./0824.mp4" width="100%" controls></video>

No app to install. No commands to memorise. You type the way you talk.

Built for the Agentic AI Workshop hackathon on **Google ADK + Gemini**, running
on **Cloud Run**, reachable through **Twilio WhatsApp**.

**Live documentation and onboarding page:**
[agentic-workshop-ten.vercel.app](https://agentic-workshop-ten.vercel.app/)
That page is what a student is given. This file is what an engineer is given.

> It fetches, explains and quizzes. It does not do the coursework and it cannot
> submit it. That boundary is enforced in code, not in a prompt.

---

## Getting in (student view)

1. Save the sandbox number **+1 415 523 8886** and send it `join strong-swung`.
   Twilio replies confirming the sandbox. This lasts 72 hours per device.
2. Send `hi`. You get a short welcome.
3. Send `link`. You get a one-time HTTPS page, valid ten minutes, where you sign
   in to the e-learning site. **Your password is never typed into the chat.**
4. Ask anything.

---

## What a student can ask

There is no command syntax. Gemini reads intent, so all of these work, in
English or Kiswahili, with typos:

| Message | What happens |
|---|---|
| *what are my units* | Their real enrolment list, with progress |
| *whats due soon* | Deadlines, accepted formats, size caps, brief documents |
| *what is <unit> about now* | Reads the lecturer's latest deck and explains it |
| *explain <topic> from our slides* | Downloads that pptx/docx/pdf, extracts the text, answers from it |
| *quiz me on <unit>* | Active recall from the real material, marked, one at a time |
| *what should I work on today* | Weakest unit and nearest deadline, turned into a plan |
| *what have I not finished in <unit>* | Outstanding activities, split by who can complete them |
| *how am I doing* | Completion percentage per unit, weakest first |
| *mark the lab as done* | Confirms first, then writes the completion |
| *remind me Friday 6pm to finish the lab* | Writes a reminder into their own calendar (asks first) |
| `more` | Sends the rest of a long answer |
| `unlink` | Token deleted immediately |

Numbers `1`-`8` are a convenience shortcut for the common ones, listed in `help`.
They are a shortcut, not the interface.

---

## Architecture

```
WhatsApp user
  |
  v
Twilio sandbox webhook  ->  FastAPI ingress          (server/main.py)
                              |
                              |  race the turn against a 13s budget
                              |  answer inside TwiML if it wins,
                              |  park it in the outbox if it does not
                              v
                            ADK agent                (browser_agent/agent.py)
                              |
                    +---------+--------
                    |                 |
                    v                 |
            Moodle REST tools         |
            per-student token         |
            (moodle.py, study.py)     |
                    |                 |
                    +---------+--------
                              v
                        TwiML reply, or `more`   (server/outbox.py)
```



## Project structure

```
.
|-- browser_agent/            the agent itself (no knowledge of WhatsApp)
|   |-- agent.py              root_agent: model, instruction, tool registry,
|   |                         ENABLE_BROWSER_TOOLS gate
|   |-- config.py             every env var, read and stripped in one place
|   |-- guardrails.py         before_tool_callback: state-changing tools stop
|   |                         and ask for YES, bound to exact arguments
|   |-- mcp_transport.py      Playwright MCP wiring, stdio locally / HTTP on
|   |                         Cloud Run, filtered tool surface
|   |-- moodle.py             REST layer: allowlist, denylist, per-student
|   |                         tokens, link/unlink, deadlines, briefs, notes
|   |-- study.py              progress, what's left, and reading the
|   |                         lecturer's real pptx/docx/pdf material
|   |-- store.py              tokens, link nonces, media ids
|   |                         (memory or Firestore, chosen by env var)
|   `-- vision.py             screenshot -> Gemini, only with browser tools on
|
|-- server/                   the interface layer (swappable)
|   |-- main.py               Twilio webhook, deterministic link/unlink,
|   |                         welcome/help/privacy/status, shortcuts, health
|   |-- format.py             markdown -> WhatsApp formatting, safely
|   |-- outbox.py             pending answers and in-flight state per sender
|   |-- runner.py             ADK sessions per sender, per-sender locks,
|   |                         concurrency cap, transient-error retry
|   |-- link.py               the single-use HTTPS sign-in page
|   |-- media.py              expiring /media/<id> proxy so the API token
|   |                         never leaves this service
|   |-- whatsapp.py           outbound send + chunking (used outside TwiML)
|   `-- telegram.py           dormant second adapter, no token configured
|
|-- web/                      the landing / documentation page deployed to
|                             Vercel with Root Directory = web
|-- deploy/                   deploy.sh (bash) and deploy.ps1 (PowerShell)
|-- docs/                     long-form documentation, see below
|-- Dockerfile                Playwright base image + Python 3.12
|-- cloudbuild.yaml           image build for Cloud Run
|-- requirements.txt
|-- .env.example              copy to .env for local runs
|
|-- my_agent/                 original workshop agent, kept as reference
`-- multi_tool_agent/         original multi-tool pattern, kept as reference
```


---

## Request lifecycle

1. Twilio POSTs the message. The webhook optionally validates the signature and
   handles `link`, `unlink`, greetings, `help`, `privacy`, `status` and the
   number shortcuts deterministically, with no model involved. Linking is the
   one step a confused model must not be able to break.
2. Everything else runs against an ADK session keyed by a salted hash of the
   sender. Per-sender locks keep one student's messages ordered; a semaphore
   caps total in-flight turns so a whole class cannot exhaust the container.
3. The turn is raced against `TWIML_BUDGET_SECONDS` (13). If it finishes, the
   answer is returned **inside the TwiML response**. If not, the student gets
   "working on it" and the finished answer waits in the outbox for `more`.
4. Replies go back as TwiML rather than through the REST API on purpose: on a
   Twilio trial account, `Messages.json` rejects free-form text with error
   `21654 ContentSid Required` and only accepts approved templates. Webhook
   replies are exempt from that gate.
5. State-changing tools return `confirmation_required`. The agent describes the
   action, waits for a plain YES, then retries the identical call.
6. Transient Gemini failures (`503 UNAVAILABLE`) are retried with backoff before
   the student ever sees an error.

---

## Safety model

| Layer | What it does |
|---|---|
| Allowlist | Only named API functions can be called at all |
| Denylist | Submission, quiz-attempt and grade functions are blocked independently, so widening the allowlist cannot accidentally unblock them |
| Confirmation gate | Anything state-changing stops and asks, bound to the exact arguments shown |
| Per-student tokens | A student's token carries only their own permissions |
| Salted keys | Store keys are hashes of the phone number, so a dump is not a contact list |
| Untrusted content | Course material is data, never instructions; a prompt hidden in a slide deck cannot redirect the agent |
| No credentials in chat | Passwords are refused in messages and only accepted on the one-time HTTPS page |
| No archive | Questions are not written to a transcript; `unlink` drops the token immediately |

---

## Deploy and run

### 1. Local, agent only (free, fastest loop)

Always run ADK from the **repo root**, never from inside an agent folder.

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in your keys
adk web                     # pick browser_agent in the dev UI
```

### 2. Local, full WhatsApp path

```bash
uvicorn server.main:app --port 8000
ngrok http 8000
```

Point the **sandbox** webhook at `<public-url>/whatsapp`, method POST, under
Messaging > Try it out > Send a WhatsApp message > **Sandbox settings**.

> Twilio exposes two different WhatsApp surfaces. The classic **sandbox**
> (`+1 415 523 8886`, join phrase, free-form replies) is the one this project
> uses. The newer trial "Try out WhatsApp" panel hands out a rotating number and
> only sends approved templates; it is a dead end for a conversational agent.

### 3. Deploy to Cloud Run

```powershell
git pull
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1
```

```bash
./deploy/deploy.sh
```

Two services are deployed: the public agent, and the private Playwright browser
service it may call when browser tools are enabled. IAM roles and the full
walkthrough are in `docs/DEPLOY_CLOUD_RUN.md`.

**`deploy.ps1` uses `--set-env-vars`, which replaces the whole environment set.**
Re-apply anything extra afterwards, for example:

```powershell
gcloud run services update whatsapp-browser-agent --region=europe-west1 `
  --update-env-vars="TWILIO_VALIDATE_SIGNATURE=0"
```

Two related traps worth knowing:

- `--update-env-vars` reuses the existing image. Only a full deploy ships new
  code.
- Cloud Run resolves `secretKeyRef: latest` when an instance **starts**, not per
  request. A new secret version needs a new revision.

### 4. Health

`GET /` is the canonical health endpoint and returns JSON. `/docs` and
`/openapi.json` also work. `/healthz` can be intercepted by the Google edge and
return a branded 404; that is cosmetic, not a container failure.

### 5. Landing page

Vercel > New Project > this repo > **Root Directory = `web`** > preset *Other*.
Live at [agentic-workshop-ten.vercel.app](https://agentic-workshop-ten.vercel.app/)
and redeployed automatically on push to `main`.

---

## Configuration

All of it lives in `browser_agent/config.py` and is documented in
`.env.example`. The ones that change behaviour most:

| Variable | Effect |
|---|---|
| `AGENT_MODEL`, `VISION_MODEL` | Model ids are env-driven, because Gemini model names get retired mid-hackathon |
| `ENABLE_BROWSER_TOOLS` | `0` (default) keeps Playwright and vision out of the tool list |
| `TWIML_BUDGET_SECONDS` | How long a turn may take before the student is asked to send `more` |
| `MAX_CONCURRENT_TURNS` | Global in-flight cap |
| `TOKEN_STORE` | `memory` or `firestore` |
| `TWILIO_VALIDATE_SIGNATURE` | `1` in production; `0` while testing with synthetic POSTs |
| `USER_KEY_PEPPER` | Salt for the sender hash. Changing it orphans every stored token |

---

## Documentation

| Document | Contents |
|---|---|
| [Live docs site](https://agentic-workshop-ten.vercel.app/) | Student-facing onboarding, what to ask, safety, architecture |
| `docs/WHATSAPP_BROWSER_AGENT.md` | Setup, demo script, known gaps |
| `docs/MULTI_USER_LINKING.md` | Link flow, threat model, memory vs Firestore |
| `docs/DEPLOY_CLOUD_RUN.md` | Cloud Run walkthrough, IAM, secrets |
| `web/README.md` | Landing page deploy and configuration |

---

## Known limitations

Stated plainly, because a demo that hides these is worse than one that doesn't:

- **Text in, text out.** Voice notes, photos and forwarded files are declined
  with an honest message. Retrieving inbound media from Twilio returns
  `20003 This feature is not available on a Trial account`; the same credentials
  return `200` on every other resource. It is an account gate, not a bug, so the
  capability is not advertised anywhere.
- **`TOKEN_STORE=memory` is single-instance**, and so is the outbox. The service
  runs pinned at one instance. Switch to `firestore` for real scale-out.
- **A new revision wipes the link.** On the memory store, redeploying mid-demo
  forces every student to `link` again.
- **Chat history is in memory.** Tokens survive a restart on Firestore;
  conversation context does not.
- **Twilio sandbox**, not a production WhatsApp sender: each device sends the
  join phrase once every 72 hours, and replies are only possible inside the
  24 hour window after a student's last message.
- **Browser tools are present but disabled** by default, for the reasons above.
- **Telegram is dormant.** The route exists, no token is configured.

---

## Credits

Google ADK, Gemini, FastAPI, Twilio, Cloud Run, Playwright MCP (in reserve).
Not affiliated with any university.

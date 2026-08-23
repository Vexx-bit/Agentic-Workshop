# Multi-user linking and concurrency

Any student can use the bot. Each one links their own e-learning account, and
sees only their own coursework.

## The flow

1. Student sends `link` on WhatsApp.
2. Server mints a single-use nonce, valid 10 minutes, and replies with
   `<PUBLIC_BASE_URL>/link/<nonce>`.
3. Student opens it and types their username and password **once**, on our
   page, over TLS.
4. Server POSTs them to `/login/token.php` and gets a web-service token back.
   The password goes out of scope in that same function: never stored, never
   logged, never echoed into the page, never in chat.
5. Token is stored under `user_key = sha256(USER_KEY_PEPPER + phone)[:32]`.
   The phone number itself is never a key.
6. Nonce is burned. Attempts are capped at 5 inside the TTL.
7. Student sends `unlink` whenever they want the token deleted.

Why not collect the password in chat: WhatsApp keeps the message on the phone
and Twilio keeps it in their logs. A password there is a password leaked. The
webhook also refuses anything that looks like a credential paste.

## Can different students use it at the same time?

Yes, for the LMS path, which is the whole demo.

| Layer | Shared? | Why it is safe |
|---|---|---|
| Moodle token | one per student | REST calls are stateless; token carries only that student's permissions |
| ADK session | one per sender | id and Moodle key are hashes of the number |
| Turn execution | one lock **per sender** | a student's own messages stay ordered; different students run concurrently |
| Total in-flight turns | capped by `MAX_CONCURRENT_TURNS` (4) | a whole class queues instead of exhausting the container |
| Playwright browser | **one shared connection** | the LMS never touches it; two simultaneous *browser* turns can share a tab |

The site reports `limitconcurrentlogins: 1`, one interactive session per user.
That is exactly why Moodle is API-only here: token calls do not consume that
session, so the bot never evicts a student from their own laptop, and a hundred
students can be served at once.

## Store backends

Tokens, nonces and file links must outlive one request: the link page can be
served by one instance and the next WhatsApp message by another.

- `TOKEN_STORE=memory` (default) - process dict. Correct **only** with a single
  instance. Pin it: `--min-instances=1 --max-instances=1`.
- `TOKEN_STORE=firestore` - one small document per record. Survives restarts and
  scale-out, free tier at classroom scale.

## Env vars

| Var | Default | Notes |
|---|---|---|
| `MOODLE_BASE_URL` | empty | site root, no trailing slash |
| `PUBLIC_BASE_URL` | empty | required for `/link` and `/media` |
| `USER_KEY_PEPPER` | empty | salt for user keys; changing it un-links everyone |
| `TOKEN_STORE` | `memory` | `memory` or `firestore` |
| `LINK_TTL_SECONDS` | `600` | link page lifetime |
| `TOKEN_TTL_SECONDS` | `2592000` | 30 days, then relink |
| `MAX_CONCURRENT_TURNS` | `4` | total in-flight agent turns |
| `TWILIO_VALIDATE_SIGNATURE` | `0` | set to `1` in production |
| `MOODLE_TOKEN` | empty | optional single-user fallback for local dev |

## Deploy (memory store, single instance)

```bash
PEPPER=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
printf '%s' "$PEPPER" > pepper.txt
gcloud secrets create USER_KEY_PEPPER --data-file=pepper.txt
rm pepper.txt

gcloud run services update whatsapp-browser-agent \
  --region=europe-west1 \
  --min-instances=1 --max-instances=1 \
  --update-env-vars=TOKEN_STORE=memory,TWILIO_VALIDATE_SIGNATURE=1 \
  --update-secrets=USER_KEY_PEPPER=USER_KEY_PEPPER:latest
```

On Windows PowerShell write secret files with
`[IO.File]::WriteAllText($path, $value)` - `Set-Content` and `Out-File` append a
newline or BOM and corrupt the secret. Verify with
`gcloud secrets versions access latest --secret=NAME | Format-Hex` and check
there is no trailing `0A`.

## Deploy (firestore store, scales out)

```bash
gcloud firestore databases create --location=europe-west1
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role=roles/datastore.user

gcloud run services update whatsapp-browser-agent \
  --region=europe-west1 \
  --update-env-vars=TOKEN_STORE=firestore
```

`GET /healthz` reports which backend is live.

## What the agent will not do

Submitting coursework, attempting a quiz and writing grades are in
`DENIED_FUNCTIONS` in `browser_agent/moodle.py`, checked independently of the
allowlist. The coursework features are read-side only: fetch the questions,
the format rules, the deadline and the lecturer's files, so the student does
the work and submits it themselves.

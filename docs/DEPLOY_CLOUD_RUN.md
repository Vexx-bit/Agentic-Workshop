# Deploying to Cloud Run

## Why two services

The obvious plan - one container running FastAPI, the ADK agent, Node, and
Chromium - is what `Dockerfile` builds, and it works. But it is *not* what the
managed deploy tooling can produce on its own:

| Path | Verdict |
|---|---|
| Deploy from a prebuilt image | Works, but something has to build the image first |
| Deploy from source archive / inline files | Dead end. Both skip the build step and use a stock Python base image: no Node, no Chromium, and dependencies must be pre-vendored |

So Playwright can never run in a source-deployed container. The topology is
split along the runtime boundary instead:

```
WhatsApp -> Twilio -> [ whatsapp-browser-agent ]  Cloud Run, public
                        FastAPI + ADK, Python
                              |  streamable HTTP + ID token
                              v
                      [ playwright-mcp ]          Cloud Run, private
                        official MS image, Node + Chromium
```

This is also better design than the single fat container: `--concurrency 1` on
the browser service gives each request its own isolated browser process
instead of fighting over one shared context behind a lock, and a browser crash
no longer takes the webhook down with it.

## Gotcha: Cloud Run will not pull from mcr.microsoft.com

Cloud Run only accepts images from `gcr.io`, `*-docker.pkg.dev`, and
`docker.io`. Pointing it straight at `mcr.microsoft.com/playwright/mcp` fails
with:

> Expected an image path like [host/]repo-path[:tag and/or @digest], where host
> is one of [region.]gcr.io, [region-]docker.pkg.dev or docker.io

The fix needs no local Docker: create an Artifact Registry **remote
repository** that proxies MCR, and pull through it.

```bash
gcloud artifacts repositories create mcr-remote \
  --repository-format=docker \
  --location=europe-west1 \
  --mode=remote-repository \
  --remote-docker-repo=https://mcr.microsoft.com \
  --description="Read-through cache for mcr.microsoft.com"
```

The browser image is then addressed as:

```
europe-west1-docker.pkg.dev/agentic-ai-workshop-506310/mcr-remote/playwright/mcp:latest
```

Artifact Registry fetches and caches it on first pull. If the deploy fails
with a permission error rather than a path error, grant the Cloud Run service
agent read access:

```bash
PROJECT_NUMBER=$(gcloud projects describe agentic-ai-workshop-506310 --format='value(projectNumber)')
gcloud artifacts repositories add-iam-policy-binding mcr-remote \
  --location=europe-west1 \
  --member="serviceAccount:service-${PROJECT_NUMBER}@serverless-robot-prod.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.reader
```

## One-shot deploy

```bash
chmod +x deploy/deploy.sh
PROJECT_ID=agentic-ai-workshop-506310 REGION=europe-west1 ./deploy/deploy.sh
```

The script creates both Artifact Registry repos (including the MCR remote),
deploys the browser service, builds and deploys the agent, wires the IAM
binding between them, and prints the webhook URL.

Before running it, create the three secrets:

```bash
for s in GOOGLE_API_KEY TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN; do
  gcloud secrets create "$s" --replication-policy=automatic 2>/dev/null || true
done
# then, for each, pipe the value in from your local .env:
printf '%s' "$YOUR_VALUE" | gcloud secrets versions add GOOGLE_API_KEY --data-file=-
```

Never pass secrets with `--set-env-vars`; they end up visible in the revision
spec and in shell history.

## Region choice

`europe-west1` is the default: full service coverage and one of the
lower-latency European regions from Nairobi. `africa-south1` (Johannesburg) is
closer geographically but thinner on services - check availability before
switching. Use the **same region** for both services and both Artifact
Registry repos, or you pay cross-region latency on every tool call.

## Auth between the services

The browser service is deployed `--no-allow-unauthenticated`. The agent mints
a Cloud Run ID token from the metadata server and sends it as a bearer token
(`browser_agent/mcp_transport.py`).

**Known limitation:** the token is fetched once, when the toolset is
constructed at process start. Cloud Run ID tokens last an hour, so a
long-lived instance will eventually get 401s from the browser service. For a
demo this is a non-issue - instances scale to zero and restart with a fresh
token. If this ever runs for real, move the token fetch into a per-request
header provider.

The fallback, if IAM proves fiddly mid-sprint, is to deploy the browser
service with `--allow-unauthenticated` **and** keep `--allowed-origins` locked
to the demo sites. That is a meaningfully weaker posture - an open browser
proxy, even a domain-restricted one - so treat it as a last resort and say so
if a judge asks.

## Verifying

```bash
gcloud run services list --region europe-west1
curl -sS "$(gcloud run services describe whatsapp-browser-agent \
  --region europe-west1 --format='value(status.url)')/healthz"
```

Then point the Twilio Sandbox webhook at `<agent-url>/whatsapp` and send a
message.

## Cost note

Both services scale to zero. The browser service is the expensive one when
warm (2 vCPU / 4 GiB), so `--max-instances 3` and `--concurrency 1` are there
to stop a runaway loop eating the $100 of credits. Chromium cold starts take
several seconds; the fast-ack pattern in `server/main.py` is what keeps that
from surfacing as a Twilio timeout.

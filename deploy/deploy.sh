#!/usr/bin/env bash
# Deploy the WhatsApp browser agent to Cloud Run.
#
# Two services, because the browser and the agent have incompatible runtimes:
#
#   1. playwright-mcp         - official Microsoft image, Node + Chromium
#   2. whatsapp-browser-agent - our FastAPI + ADK container
#
# The agent reaches the browser over streamable HTTP, so the identical code
# runs locally against a stdio subprocess with no changes.
#
# NOTE ON THE BROWSER IMAGE: Cloud Run refuses to pull from
# mcr.microsoft.com - it only accepts gcr.io, *-docker.pkg.dev and docker.io.
# So we stand up an Artifact Registry *remote repository* that proxies MCR.
# No local Docker needed; Artifact Registry does the mirroring on first pull.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-agentic-ai-workshop-506310}"
REGION="${REGION:-europe-west1}"
REPO="${REPO:-agents}"
MCR_REPO="${MCR_REPO:-mcr-remote}"
AGENT_SERVICE="${AGENT_SERVICE:-whatsapp-browser-agent}"
MCP_SERVICE="${MCP_SERVICE:-playwright-mcp}"

AGENT_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${AGENT_SERVICE}:latest"
MCP_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${MCR_REPO}/playwright/mcp:latest"

# Restrict what the browser is allowed to reach. This is the main mitigation
# if you ever make the browser service public.
ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-https://www.saucedemo.com;https://the-internet.herokuapp.com}"

gcloud config set project "${PROJECT_ID}"

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com

echo "==> Ensuring Artifact Registry repos exist"
gcloud artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker --location "${REGION}" \
    --description "Hackathon agent images"

# Remote repo proxying mcr.microsoft.com, so Cloud Run can pull Playwright.
gcloud artifacts repositories describe "${MCR_REPO}" --location "${REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${MCR_REPO}" \
    --repository-format=docker --location "${REGION}" \
    --mode=remote-repository \
    --remote-docker-repo="https://mcr.microsoft.com" \
    --description "Read-through cache for mcr.microsoft.com"

echo "==> 1/3 Deploying the browser (Playwright MCP) service"
# Private by default: only the agent's service account may invoke it.
# --concurrency 1 gives every request its own isolated browser process.
gcloud run deploy "${MCP_SERVICE}" \
  --image "${MCP_IMAGE}" \
  --region "${REGION}" \
  --no-allow-unauthenticated \
  --cpu 2 --memory 4Gi \
  --concurrency 1 \
  --timeout 600 \
  --max-instances 3 \
  --port 8080 \
  --args "--headless,--isolated,--browser,chromium,--caps,vision,--image-responses,omit,--viewport-size,1280x900,--port,8080,--host,0.0.0.0,--allowed-origins,${ALLOWED_ORIGINS}"

MCP_URL="$(gcloud run services describe "${MCP_SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo "    browser service: ${MCP_URL}"

echo "==> 2/3 Building the agent image"
gcloud builds submit --config cloudbuild.yaml \
  --substitutions="_REGION=${REGION},_REPO=${REPO},_IMAGE=${AGENT_SERVICE}"

echo "==> 3/3 Deploying the agent service"
# Public, because Twilio must be able to POST the webhook. Signature
# validation in server/main.py is what actually protects the endpoint, so keep
# TWILIO_VALIDATE_SIGNATURE=1 here.
gcloud run deploy "${AGENT_SERVICE}" \
  --image "${AGENT_IMAGE}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --cpu 1 --memory 1Gi \
  --timeout 600 \
  --max-instances 5 \
  --set-env-vars "PLAYWRIGHT_MCP_URL=${MCP_URL}/mcp,BROWSER_ARTIFACT_DIR=/tmp/artifacts,TWILIO_VALIDATE_SIGNATURE=1" \
  --set-secrets "GOOGLE_API_KEY=GOOGLE_API_KEY:latest,TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest"

AGENT_URL="$(gcloud run services describe "${AGENT_SERVICE}" --region "${REGION}" --format='value(status.url)')"

echo "==> Granting the agent permission to call the browser service"
AGENT_SA="$(gcloud run services describe "${AGENT_SERVICE}" --region "${REGION}" --format='value(spec.template.spec.serviceAccountName)')"
if [ -z "${AGENT_SA}" ]; then
  PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
  AGENT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
fi
gcloud run services add-iam-policy-binding "${MCP_SERVICE}" \
  --region "${REGION}" \
  --member "serviceAccount:${AGENT_SA}" \
  --role roles/run.invoker

# Twilio signature validation needs the agent's own public URL.
gcloud run services update "${AGENT_SERVICE}" --region "${REGION}" \
  --update-env-vars "PUBLIC_BASE_URL=${AGENT_URL}"

cat <<EOF

Done.

  agent service : ${AGENT_URL}
  webhook URL   : ${AGENT_URL}/whatsapp
  browser svc   : ${MCP_URL} (private)

Next: paste the webhook URL into the Twilio WhatsApp Sandbox settings under
"When a message comes in" (HTTP POST), then message the sandbox number.
EOF

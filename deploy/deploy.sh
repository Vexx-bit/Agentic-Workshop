#!/usr/bin/env bash
# Deploy the WhatsApp browser agent to Cloud Run.
#
# Two services, because the browser and the agent have incompatible runtimes:
#
#   1. playwright-mcp        - official Microsoft image, Node + Chromium
#   2. whatsapp-browser-agent - our FastAPI + ADK container
#
# The agent reaches the browser over streamable HTTP, so the same code runs
# locally against a stdio subprocess with no changes.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-agentic-ai-workshop-506310}"
REGION="${REGION:-europe-west1}"
REPO="${REPO:-agents}"
AGENT_SERVICE="${AGENT_SERVICE:-whatsapp-browser-agent}"
MCP_SERVICE="${MCP_SERVICE:-playwright-mcp}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${AGENT_SERVICE}:latest"

# Restrict what the browser is allowed to reach. This is the main mitigation
# if you ever make the browser service public.
ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-https://www.saucedemo.com;https://the-internet.herokuapp.com}"

gcloud config set project "${PROJECT_ID}"

echo "==> Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com

echo "==> Ensuring Artifact Registry repo exists"
gcloud artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker --location "${REGION}" \
    --description "Hackathon agent images"

echo "==> 1/3 Deploying the browser (Playwright MCP) service"
# Private by default: only the agent's service account may invoke it.
gcloud run deploy "${MCP_SERVICE}" \
  --image mcr.microsoft.com/playwright/mcp:latest \
  --region "${REGION}" \
  --no-allow-unauthenticated \
  --cpu 2 --memory 4Gi \
  --concurrency 1 \
  --timeout 600 \
  --max-instances 3 \
  --args "--headless,--isolated,--browser,chromium,--caps,vision,--image-responses,omit,--port,8080,--host,0.0.0.0,--allowed-origins,${ALLOWED_ORIGINS}"

MCP_URL="$(gcloud run services describe "${MCP_SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo "    browser service: ${MCP_URL}"

echo "==> 2/3 Building the agent image"
gcloud builds submit --config cloudbuild.yaml \
  --substitutions="_REGION=${REGION},_REPO=${REPO},_IMAGE=${AGENT_SERVICE}"

echo "==> 3/3 Deploying the agent service"
# Public, because Twilio must be able to POST the webhook. Signature
# validation in server/main.py is what actually protects the endpoint, so keep
# TWILIO_VALIDATE_SIGNATURE=1 in production.
gcloud run deploy "${AGENT_SERVICE}" \
  --image "${IMAGE}" \
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

# Twilio needs the public URL of the agent for signature validation.
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

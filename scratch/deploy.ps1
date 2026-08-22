$ErrorActionPreference = "Continue"

$PROJECT_ID = "agentic-ai-workshop-506310"
$REGION = "europe-west1"
$REPO = "agents"
$MCR_REPO = "mcr-remote"
$AGENT_SERVICE = "whatsapp-browser-agent"
$MCP_SERVICE = "playwright-mcp"

$MCP_CPU = "1"
$MCP_MEMORY = "2Gi"
$MCP_MAX_INSTANCES = "1"
$AGENT_CPU = "1"
$AGENT_MEMORY = "512Mi"
$AGENT_MAX_INSTANCES = "2"

$AGENT_IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$AGENT_SERVICE`:latest"
$MCP_IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$MCR_REPO/playwright/mcp:latest"

$ALLOWED_ORIGINS = "https://www.saucedemo.com;https://the-internet.herokuapp.com"

gcloud config set project $PROJECT_ID

Write-Host "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

Write-Host "==> Ensuring Artifact Registry repos exist"
gcloud artifacts repositories describe $REPO --location $REGION 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $REPO --repository-format=docker --location $REGION --description "Hackathon agent images"
}

gcloud artifacts repositories describe $MCR_REPO --location $REGION 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $MCR_REPO --repository-format=docker --location $REGION --mode=remote-repository --remote-docker-repo="https://mcr.microsoft.com" --description "Read-through cache for mcr.microsoft.com"
}

$ARGS_STRING = "--headless,--isolated,--browser,chromium,--caps,vision,--image-responses,omit,--viewport-size,1280x900,--port,8080,--host,0.0.0.0,--allowed-origins,$ALLOWED_ORIGINS"
gcloud run deploy $MCP_SERVICE --image $MCP_IMAGE --region $REGION --no-allow-unauthenticated --cpu $MCP_CPU --memory $MCP_MEMORY --concurrency 1 --timeout 600 --max-instances $MCP_MAX_INSTANCES --port 8080 --args=$ARGS_STRING

$MCP_URL = (gcloud run services describe $MCP_SERVICE --region $REGION --format="value(status.url)")
Write-Host "    browser service: $MCP_URL"

Write-Host "==> 2/3 Building the agent image"
gcloud builds submit --config cloudbuild.yaml --substitutions="_REGION=$REGION,_REPO=$REPO,_IMAGE=$AGENT_SERVICE"

Write-Host "==> 3/3 Deploying the agent service"
gcloud run deploy $AGENT_SERVICE --image $AGENT_IMAGE --region $REGION --allow-unauthenticated --cpu $AGENT_CPU --memory $AGENT_MEMORY --timeout 600 --max-instances $AGENT_MAX_INSTANCES --set-env-vars "PLAYWRIGHT_MCP_URL=$MCP_URL/mcp,BROWSER_ARTIFACT_DIR=/tmp/artifacts,TWILIO_VALIDATE_SIGNATURE=1" --set-secrets "GOOGLE_API_KEY=GOOGLE_API_KEY:latest,TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest"

$AGENT_URL = (gcloud run services describe $AGENT_SERVICE --region $REGION --format="value(status.url)")

Write-Host "==> Granting the agent permission to call the browser service"
$AGENT_SA = (gcloud run services describe $AGENT_SERVICE --region $REGION --format="value(spec.template.spec.serviceAccountName)")
if (-not $AGENT_SA) {
    $PROJECT_NUMBER = (gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
    $AGENT_SA = "$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
}
gcloud run services add-iam-policy-binding $MCP_SERVICE --region $REGION --member "serviceAccount:$AGENT_SA" --role roles/run.invoker

gcloud run services update $AGENT_SERVICE --region $REGION --update-env-vars "PUBLIC_BASE_URL=$AGENT_URL"

Write-Host ""
Write-Host "Done."
Write-Host "  agent service : $AGENT_URL"
Write-Host "  webhook URL   : $AGENT_URL/whatsapp"
Write-Host "  browser svc   : $MCP_URL (private)"

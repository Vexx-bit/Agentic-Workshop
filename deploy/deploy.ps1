# Deploys both Cloud Run services: the private browser service first, then the
# public agent that calls it.
#
# Run from the repo root:  .\deploy\deploy.ps1
#
# Prerequisites: gcloud authenticated, and these secrets present in Secret
# Manager - GOOGLE_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
# MOODLE_TOKEN, USER_KEY_PEPPER. See docs/DEPLOY_CLOUD_RUN.md.
#
# HARD RULE, learned the expensive way: service URLs are only ever read back
# from gcloud. Never type, guess, paste or reconstruct a *.run.app hostname.
# A wrong one is accepted silently by Cloud Run and only surfaces as a 404
# when a student taps their link page.

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

# TOKEN_STORE=memory keeps linked students in the instance's own memory, so the
# service must stay pinned to exactly one instance. Switch to firestore before
# raising these. Set min-instances back to 0 after the demo to stop paying for
# an idle container.
$AGENT_MIN_INSTANCES = "1"
$AGENT_MAX_INSTANCES = "1"

# Assembled from parts on purpose: a pasted URL once corrupted the Dockerfile.
$MCR_HOST = "https:" + "//mcr.microsoft.com"
$MOODLE_BASE_URL = "https:" + "//elearning.zetech.ac.ke"
$ALLOWED_ORIGINS = ("https:" + "//www.saucedemo.com") + ";" + ("https:" + "//the-internet.herokuapp.com")

$AGENT_IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$AGENT_SERVICE`:latest"
$MCP_IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$MCR_REPO/playwright/mcp:latest"

# Reads a service's live URI from gcloud and refuses to return anything that is
# not plausibly a Cloud Run hostname for this project. Empty output from a
# --format query is the dangerous case: it silently becomes an empty env var.
function Get-ServiceUri([string]$Service) {
    $uri = (gcloud run services describe $Service --region $REGION --format="value(status.url)")
    if (-not $uri) {
        # Newer gcloud releases expose it as uri rather than status.url.
        $uri = (gcloud run services describe $Service --region $REGION --format="value(uri)")
    }
    $uri = "$uri".Trim()

    if (-not $uri) {
        throw "Could not read a URL for '$Service'. Refusing to deploy with an empty URL."
    }
    if ($uri -notmatch "^https://[a-z0-9-]+\.[a-z0-9-]*\.?run\.app/?$") {
        throw "'$Service' returned an implausible URL: '$uri'. Refusing to continue."
    }

    return $uri.TrimEnd("/")
}

gcloud config set project $PROJECT_ID

Write-Host "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

Write-Host "==> Ensuring Artifact Registry repos exist"
gcloud artifacts repositories describe $REPO --location $REGION 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $REPO --repository-format=docker --location $REGION --description "Hackathon agent images"
}

# Cloud Run only accepts images from gcr.io, docker.pkg.dev or docker.io, so the
# Playwright image is pulled through a remote (read-through) repository.
gcloud artifacts repositories describe $MCR_REPO --location $REGION 2>$null
if ($LASTEXITCODE -ne 0) {
    gcloud artifacts repositories create $MCR_REPO --repository-format=docker --location $REGION --mode=remote-repository --remote-docker-repo="$MCR_HOST" --description "Read-through cache for mcr.microsoft.com"
}

Write-Host "==> 1/3 Deploying the browser service (private)"
$ARGS_STRING = "--headless,--isolated,--browser,chromium,--caps,vision,--image-responses,omit,--viewport-size,1280x900,--port,8080,--host,0.0.0.0,--allowed-origins,$ALLOWED_ORIGINS"
gcloud run deploy $MCP_SERVICE --image $MCP_IMAGE --region $REGION --no-allow-unauthenticated --cpu $MCP_CPU --memory $MCP_MEMORY --concurrency 1 --timeout 600 --max-instances $MCP_MAX_INSTANCES --port 8080 --args=$ARGS_STRING

$MCP_URL = Get-ServiceUri $MCP_SERVICE
Write-Host "    browser service: $MCP_URL"

Write-Host "==> 2/3 Building the agent image"
gcloud builds submit --config cloudbuild.yaml --substitutions="_REGION=$REGION,_REPO=$REPO,_IMAGE=$AGENT_SERVICE"

Write-Host "==> 3/3 Deploying the agent service"
gcloud run deploy $AGENT_SERVICE --image $AGENT_IMAGE --region $REGION --allow-unauthenticated --cpu $AGENT_CPU --memory $AGENT_MEMORY --timeout 600 --min-instances $AGENT_MIN_INSTANCES --max-instances $AGENT_MAX_INSTANCES --set-env-vars "PLAYWRIGHT_MCP_URL=$MCP_URL/mcp,BROWSER_ARTIFACT_DIR=/tmp/artifacts,TWILIO_VALIDATE_SIGNATURE=1,MOODLE_BASE_URL=$MOODLE_BASE_URL,TOKEN_STORE=memory" --set-secrets "GOOGLE_API_KEY=GOOGLE_API_KEY:latest,TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest,MOODLE_TOKEN=MOODLE_TOKEN:latest,USER_KEY_PEPPER=USER_KEY_PEPPER:latest"

$AGENT_URL = Get-ServiceUri $AGENT_SERVICE

Write-Host "==> Granting the agent permission to call the browser service"
$AGENT_SA = (gcloud run services describe $AGENT_SERVICE --region $REGION --format="value(spec.template.spec.serviceAccountName)")
if (-not $AGENT_SA) {
    $PROJECT_NUMBER = (gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
    $AGENT_SA = "$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
}
gcloud run services add-iam-policy-binding $MCP_SERVICE --region $REGION --member "serviceAccount:$AGENT_SA" --role roles/run.invoker

# The service cannot know its own URL until it exists, so the link page's base
# URL is injected in a second revision.
gcloud run services update $AGENT_SERVICE --region $REGION --update-env-vars "PUBLIC_BASE_URL=$AGENT_URL"

# Observe the health endpoint rather than assuming it. link_store proves the
# token store actually initialised; "the route exists" does not.
Write-Host ""
Write-Host "==> Verifying /healthz"
try {
    $health = Invoke-RestMethod -Uri "$AGENT_URL/healthz" -TimeoutSec 45
    Write-Host "    status     : $($health.status)"
    Write-Host "    link_store : $($health.link_store)"
    if ($health.link_store -ne "memory" -and $health.link_store -ne "firestore") {
        Write-Host "    WARNING: unexpected link_store value. Students may not stay linked."
    }
} catch {
    Write-Host "    FAILED to reach $AGENT_URL/healthz"
    Write-Host "    $($_.Exception.Message)"
    Write-Host "    Do not demo until this returns JSON."
}

Write-Host ""
Write-Host "Done."
Write-Host "  agent service : $AGENT_URL"
Write-Host "  webhook URL   : $AGENT_URL/whatsapp"
Write-Host "  link page     : $AGENT_URL/link/<nonce>"
Write-Host "  browser svc   : $MCP_URL (private)"

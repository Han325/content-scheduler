# Deploy Lineup to Cloud Run with GCS persistent storage + Cloud Scheduler.
# Run from the project root after: gcloud auth login
#
# Usage:
#   .\deploy.ps1           # first deploy (prompts for credentials)
#   .\deploy.ps1 -Update   # redeploy after code changes

param(
    [switch]$Update,
    [string]$Domain = ""
)

$ErrorActionPreference = "Stop"

$PROJECT  = (gcloud config get-value project).Trim()
$REGION   = "asia-southeast1"
$TIMEZONE = "Asia/Kuala_Lumpur"
$SERVICE  = "lineup"
$IMAGE    = "gcr.io/$PROJECT/$SERVICE"
$BUCKET   = "$PROJECT-lineup-data"

Write-Host "Project  : $PROJECT"
Write-Host "Region   : $REGION"
Write-Host "Timezone : $TIMEZONE"
Write-Host "Image    : $IMAGE"
Write-Host "Bucket   : $BUCKET"
Write-Host ""

# -- 1. Enable required APIs --------------------------------------------------
Write-Host "Enabling required GCP APIs..."
gcloud services enable `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    cloudscheduler.googleapis.com `
    secretmanager.googleapis.com `
    storage.googleapis.com `
    --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: Could not enable APIs. Make sure billing is linked:"
    Write-Host "  gcloud billing projects link $PROJECT --billing-account=YOUR_BILLING_ACCOUNT_ID"
    exit 1
}

# -- 2. Create GCS bucket -----------------------------------------------------
$bucketExists = $false
try { $null = gsutil ls -b "gs://$BUCKET" 2>&1; $bucketExists = ($LASTEXITCODE -eq 0) } catch { $bucketExists = $false }
if (-not $bucketExists) {
    Write-Host "Creating bucket gs://$BUCKET ..."
    gsutil mb -l $REGION "gs://$BUCKET"
    gsutil uniformbucketlevelaccess set on "gs://$BUCKET"
} else {
    Write-Host "Bucket gs://$BUCKET already exists."
}

# -- 3. Build and push image --------------------------------------------------
Write-Host "Building and pushing image..."
gcloud builds submit --tag $IMAGE .

# -- 4. Store secrets in Secret Manager ---------------------------------------
function Store-Secret([string]$Name, [string]$Value) {
    $exists = $false
    try { $null = gcloud secrets describe $Name 2>&1; $exists = ($LASTEXITCODE -eq 0) } catch { $exists = $false }
    if ($exists) {
        Write-Host "Secret '$Name' already exists - skipping."
    } else {
        $tmp = [System.IO.Path]::GetTempFileName()
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($Value)
        [System.IO.File]::WriteAllBytes($tmp, $bytes)
        gcloud secrets create $Name --data-file=$tmp
        Remove-Item $tmp
        Write-Host "Secret '$Name' created."
    }
}

if (-not $Update) {
    $CLIENT_ID     = Read-Host "Enter YOUTUBE_CLIENT_ID"
    $CLIENT_SECRET = Read-Host "Enter YOUTUBE_CLIENT_SECRET"
    Store-Secret "lineup-yt-client-id"     $CLIENT_ID
    Store-Secret "lineup-yt-client-secret" $CLIENT_SECRET

    $CRON_SECRET = (python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
    Store-Secret "lineup-cron-secret" $CRON_SECRET
    Write-Host "Cron secret generated and stored."

    $SESSION_SECRET = (python -c "import secrets; print(secrets.token_urlsafe(48))").Trim()
    Store-Secret "lineup-session-secret" $SESSION_SECRET
    Write-Host "Session secret generated and stored."

    $APP_PASSWORD = Read-Host "Choose an app password (protects the UI, leave blank to disable)"
    Store-Secret "lineup-app-password" $APP_PASSWORD
} else {
    $CRON_SECRET     = (gcloud secrets versions access latest --secret="lineup-cron-secret").Trim()
    $SESSION_SECRET  = (gcloud secrets versions access latest --secret="lineup-session-secret").Trim()
    $APP_PASSWORD    = (gcloud secrets versions access latest --secret="lineup-app-password").Trim()
}

# -- 5. Grant required permissions to the Cloud Run service account -----------
Write-Host "Granting permissions to Cloud Run service account..."
$PROJECT_NUMBER = (gcloud projects describe $PROJECT --format "value(projectNumber)").Trim()
$SA = "$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT `
    --member "serviceAccount:$SA" `
    --role "roles/secretmanager.secretAccessor" `
    --quiet

gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" `
    --member "serviceAccount:$SA" `
    --role "roles/storage.objectAdmin" `
    --quiet

# -- 6. Deploy to Cloud Run ---------------------------------------------------
Write-Host "Deploying to Cloud Run..."
gcloud run deploy $SERVICE `
    --image $IMAGE `
    --region $REGION `
    --platform managed `
    --execution-environment gen2 `
    --memory 512Mi `
    --cpu 1 `
    --min-instances 0 `
    --max-instances 1 `
    --timeout 540 `
    --add-volume "name=data,type=cloud-storage,bucket=$BUCKET" `
    --add-volume-mount "volume=data,mount-path=/mnt/data" `
    --set-env-vars "DATABASE_URL=sqlite:////tmp/lineup.db,GCS_DB_PATH=/mnt/data/lineup.db,TOKEN_FILE=/mnt/data/token.json,DISABLE_SCHEDULER=true,TZ=Asia/Kuala_Lumpur" `
    --set-secrets "YOUTUBE_CLIENT_ID=lineup-yt-client-id:latest,YOUTUBE_CLIENT_SECRET=lineup-yt-client-secret:latest,CRON_SECRET=lineup-cron-secret:latest,APP_PASSWORD=lineup-app-password:latest,SESSION_SECRET=lineup-session-secret:latest" `
    --allow-unauthenticated

# -- 6. Grab the service URL --------------------------------------------------
$SERVICE_URL = (gcloud run services describe $SERVICE `
    --region $REGION `
    --format "value(status.url)").Trim()

if ($SERVICE_URL -eq "") {
    Write-Host "ERROR: Deployment failed - could not retrieve service URL. Check errors above."
    exit 1
}
Write-Host "Service URL: $SERVICE_URL"

gcloud run services update $SERVICE `
    --region $REGION `
    --update-env-vars "OAUTH_REDIRECT_URI=$SERVICE_URL/auth/callback" `
    --quiet

# -- 7. Create or update Cloud Scheduler job ----------------------------------
Write-Host "Setting up Cloud Scheduler (18:00 $TIMEZONE daily)..."
$jobExists = $false
try { $null = gcloud scheduler jobs describe lineup-daily-refresh --location $REGION 2>&1; $jobExists = ($LASTEXITCODE -eq 0) } catch { $jobExists = $false }
if ($jobExists) {
    gcloud scheduler jobs update http lineup-daily-refresh `
        --location $REGION `
        --schedule "0 18 * * *" `
        --time-zone $TIMEZONE `
        "--uri=$SERVICE_URL/refresh" `
        --http-method POST `
        "--update-headers=X-Cron-Secret=$CRON_SECRET" `
        --quiet
    Write-Host "Cloud Scheduler job updated."
} else {
    gcloud scheduler jobs create http lineup-daily-refresh `
        --location $REGION `
        --schedule "0 18 * * *" `
        --time-zone $TIMEZONE `
        "--uri=$SERVICE_URL/refresh" `
        --http-method POST `
        "--headers=X-Cron-Secret=$CRON_SECRET" `
        --quiet
    Write-Host "Cloud Scheduler job created."
}

# -- 8. Custom domain (optional) ----------------------------------------------
if ($Domain -ne "") {
    Write-Host "Setting up custom domain: $Domain ..."
    $null = gcloud run domain-mappings describe --domain $Domain --region $REGION 2>&1
    if ($LASTEXITCODE -ne 0) {
        gcloud run domain-mappings create --service $SERVICE --domain $Domain --region $REGION
    }
    Write-Host ""
    Write-Host "DNS records to add in Cloudflare for $Domain :"
    gcloud run domain-mappings describe --domain $Domain --region $REGION --format "table(status.resourceRecords[].name, status.resourceRecords[].rrdata, status.resourceRecords[].type)"
    Write-Host ""
    Write-Host "Once DNS propagates, update the OAuth redirect URI in Google Cloud Console to:"
    Write-Host "  https://$Domain/auth/callback"
    Write-Host "Then redeploy with:"
    Write-Host "  .\deploy.ps1 -Update -Domain $Domain"

    # Update OAUTH_REDIRECT_URI to the custom domain
    gcloud run services update $SERVICE `
        --region $REGION `
        --update-env-vars "OAUTH_REDIRECT_URI=https://$Domain/auth/callback" `
        --quiet
}

# -- 9. Done ------------------------------------------------------------------
Write-Host ""
Write-Host "========================================================"
Write-Host "  Deployment complete!"
Write-Host "========================================================"
Write-Host ""
Write-Host "  App URL : $SERVICE_URL"
Write-Host ""
Write-Host "  Next steps:"
Write-Host "  1. Add this OAuth redirect URI in Google Cloud Console:"
Write-Host "     $SERVICE_URL/auth/callback"
Write-Host ""
Write-Host "  2. Visit $SERVICE_URL/auth to authenticate with YouTube."
Write-Host ""
Write-Host "  3. Add the OAuth redirect URI in Google Cloud Console (APIs & Services > Credentials)
     $SERVICE_URL/auth/callback

  4. Run the first curation manually:"
Write-Host "     Invoke-RestMethod -Method Post -Uri '$SERVICE_URL/refresh' -Headers @{ 'X-Cron-Secret' = '$CRON_SECRET' }"
Write-Host ""
Write-Host "  After that, Cloud Scheduler fires daily at 18:00 $TIMEZONE."
Write-Host "========================================================"

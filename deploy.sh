#!/usr/bin/env bash
# Deploy Lineup to Cloud Run with GCS persistent storage + Cloud Scheduler.
# Run from the project root after: gcloud auth login && gcloud config set project YOUR_PROJECT
#
# Usage:
#   ./deploy.sh              # first deploy (prompts for credentials)
#   ./deploy.sh --update     # redeploy after code changes (skips credential prompts)

set -euo pipefail

PROJECT=$(gcloud config get-value project)
REGION="asia-southeast1"          # change to your preferred region
TIMEZONE="Asia/Kuala_Lumpur"      # change to your local timezone
SERVICE="lineup"
IMAGE="gcr.io/$PROJECT/$SERVICE"
BUCKET="$PROJECT-lineup-data"

echo "Project  : $PROJECT"
echo "Region   : $REGION"
echo "Timezone : $TIMEZONE"
echo "Image    : $IMAGE"
echo "Bucket   : $BUCKET"
echo ""

# ── 1. Enable required APIs ──────────────────────────────────────────────────
echo "Enabling required GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com \
  --quiet

# ── 2. Create GCS bucket for persistent data ────────────────────────────────
if ! gsutil ls -b "gs://$BUCKET" &>/dev/null; then
  echo "Creating bucket gs://$BUCKET ..."
  gsutil mb -l "$REGION" "gs://$BUCKET"
  gsutil uniformbucketlevelaccess set on "gs://$BUCKET"
else
  echo "Bucket gs://$BUCKET already exists."
fi

# ── 3. Build & push image ───────────────────────────────────────────────────
echo "Building and pushing image..."
gcloud builds submit --tag "$IMAGE" .

# ── 4. Store secrets in Secret Manager ──────────────────────────────────────
store_secret() {
  local name=$1 value=$2
  if ! gcloud secrets describe "$name" &>/dev/null; then
    echo -n "$value" | gcloud secrets create "$name" --data-file=-
    echo "Secret '$name' created."
  else
    echo "Secret '$name' already exists — skipping."
  fi
}

if [[ "${1:-}" != "--update" ]]; then
  read -rp "Enter YOUTUBE_CLIENT_ID: " CLIENT_ID
  read -rp "Enter YOUTUBE_CLIENT_SECRET: " CLIENT_SECRET
  store_secret "lineup-yt-client-id" "$CLIENT_ID"
  store_secret "lineup-yt-client-secret" "$CLIENT_SECRET"

  # Generate a random cron secret so only Cloud Scheduler can hit /refresh
  CRON_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  store_secret "lineup-cron-secret" "$CRON_SECRET"
  echo "Cron secret generated and stored."
else
  # Read existing cron secret for Cloud Scheduler update
  CRON_SECRET=$(gcloud secrets versions access latest --secret="lineup-cron-secret")
fi

# ── 5. Deploy to Cloud Run ───────────────────────────────────────────────────
echo "Deploying to Cloud Run..."
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --execution-environment gen2 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 1 \
  --add-volume "name=data,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount "volume=data,mount-path=/mnt/data" \
  --set-env-vars "DATABASE_URL=sqlite:////mnt/data/lineup.db,TOKEN_FILE=/mnt/data/token.json,DISABLE_SCHEDULER=true" \
  --set-secrets "YOUTUBE_CLIENT_ID=lineup-yt-client-id:latest,YOUTUBE_CLIENT_SECRET=lineup-yt-client-secret:latest,CRON_SECRET=lineup-cron-secret:latest" \
  --allow-unauthenticated

# ── 6. Grab the service URL ──────────────────────────────────────────────────
SERVICE_URL=$(gcloud run services describe "$SERVICE" \
  --region "$REGION" \
  --format "value(status.url)")
echo "Service URL: $SERVICE_URL"

# Set OAUTH_REDIRECT_URI now that we know the URL
gcloud run services update "$SERVICE" \
  --region "$REGION" \
  --update-env-vars "OAUTH_REDIRECT_URI=$SERVICE_URL/auth/callback" \
  --quiet

# ── 7. Create / update Cloud Scheduler job ──────────────────────────────────
echo "Setting up Cloud Scheduler (18:00 $TIMEZONE daily)..."
if gcloud scheduler jobs describe lineup-daily-refresh --location "$REGION" &>/dev/null; then
  gcloud scheduler jobs update http lineup-daily-refresh \
    --location "$REGION" \
    --schedule "0 18 * * *" \
    --time-zone "$TIMEZONE" \
    --uri "$SERVICE_URL/refresh" \
    --http-method POST \
    --headers "X-Cron-Secret=$CRON_SECRET" \
    --quiet
  echo "Cloud Scheduler job updated."
else
  gcloud scheduler jobs create http lineup-daily-refresh \
    --location "$REGION" \
    --schedule "0 18 * * *" \
    --time-zone "$TIMEZONE" \
    --uri "$SERVICE_URL/refresh" \
    --http-method POST \
    --headers "X-Cron-Secret=$CRON_SECRET" \
    --quiet
  echo "Cloud Scheduler job created."
fi

# ── 8. Post-deploy instructions ──────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Deployment complete!"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  App URL : $SERVICE_URL"
echo ""
echo "  Next steps:"
echo "  1. Add this OAuth redirect URI in Google Cloud Console:"
echo "     $SERVICE_URL/auth/callback"
echo ""
echo "  2. Visit $SERVICE_URL/auth to authenticate with YouTube."
echo ""
echo "  3. Run the first curation manually:"
echo "     curl -X POST $SERVICE_URL/refresh \\"
echo "          -H 'X-Cron-Secret: $CRON_SECRET'"
echo ""
echo "  After that, Cloud Scheduler fires daily at 18:00 $TIMEZONE."
echo "════════════════════════════════════════════════════════"

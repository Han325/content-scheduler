#!/usr/bin/env bash
# One-time setup + deploy to Cloud Run with GCS persistent volume.
# Run this from the project root after `gcloud auth login`.
#
# Usage:
#   ./deploy.sh                          # first deploy
#   ./deploy.sh --update                 # redeploy after code changes

set -euo pipefail

PROJECT=$(gcloud config get-value project)
REGION="us-central1"
SERVICE="lineup"
IMAGE="gcr.io/$PROJECT/$SERVICE"
BUCKET="$PROJECT-lineup-data"

echo "Project : $PROJECT"
echo "Region  : $REGION"
echo "Image   : $IMAGE"
echo "Bucket  : $BUCKET"
echo ""

# ── 1. Create GCS bucket (skips if already exists) ──────────────────────────
if ! gsutil ls -b "gs://$BUCKET" &>/dev/null; then
  echo "Creating bucket gs://$BUCKET ..."
  gsutil mb -l "$REGION" "gs://$BUCKET"
  gsutil uniformbucketlevelaccess set on "gs://$BUCKET"
else
  echo "Bucket gs://$BUCKET already exists."
fi

# ── 2. Build & push image ───────────────────────────────────────────────────
echo "Building and pushing image..."
gcloud builds submit --tag "$IMAGE" .

# ── 3. Store YouTube credentials in Secret Manager ──────────────────────────
# Only prompts on first run; skips if secrets already exist.
store_secret() {
  local name=$1 value=$2
  if ! gcloud secrets describe "$name" &>/dev/null; then
    echo -n "$value" | gcloud secrets create "$name" --data-file=-
    echo "Secret $name created."
  else
    echo "Secret $name already exists."
  fi
}

if [[ "${1:-}" != "--update" ]]; then
  read -rp "Enter YOUTUBE_CLIENT_ID: " CLIENT_ID
  read -rp "Enter YOUTUBE_CLIENT_SECRET: " CLIENT_SECRET
  store_secret "youtube-client-id" "$CLIENT_ID"
  store_secret "youtube-client-secret" "$CLIENT_SECRET"
fi

# ── 4. Deploy to Cloud Run ───────────────────────────────────────────────────
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
  --set-env-vars "DATABASE_URL=sqlite:////mnt/data/lineup.db,TOKEN_FILE=/mnt/data/token.json" \
  --set-secrets "YOUTUBE_CLIENT_ID=youtube-client-id:latest,YOUTUBE_CLIENT_SECRET=youtube-client-secret:latest" \
  --allow-unauthenticated

echo ""
echo "Done. Visit the URL above, then go to /auth to authenticate with YouTube."
echo "After auth, POST /refresh to build your first lineup."

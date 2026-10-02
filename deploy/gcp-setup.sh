#!/usr/bin/env bash
#
# Provision a GCP e2-micro VM for the bot.
#
# e2-micro is the only instance in the Always Free tier: 1 GB RAM, 30 GB disk,
# free forever in us-west1 / us-central1 / us-east1. One such instance per
# project, billed by time used and capped at one month per calendar month.
#
# Usage:
#   export GCP_PROJECT_ID=my-project-123
#   ./deploy/gcp-setup.sh
#
# Re-running is safe: existing resources are detected and left alone.
#
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-}"
ZONE="${GCP_ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-tezos-offers-bot}"
SSH_USER="${SSH_USER:-${USER:-root}}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "error: set GCP_PROJECT_ID first, e.g. export GCP_PROJECT_ID=my-project-123" >&2
  exit 1
fi

if ! command -v gcloud >/dev/null 2>&1; then
  echo "error: gcloud CLI not found. Install it with:" >&2
  echo "  echo 'deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main' | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list" >&2
  echo "  sudo apt-get update && sudo apt-get install -y google-cloud-cli" >&2
  exit 1
fi

gcloud config set project "$PROJECT_ID" --quiet
echo "==> Project: $PROJECT_ID   Zone: $ZONE   VM: $VM_NAME"

echo "==> Enabling the Compute Engine API"
gcloud services enable compute.googleapis.com --quiet

# --- SSH firewall rule -------------------------------------------------
# Scoped to this machine's public IP rather than 0.0.0.0/0. The bot itself
# exposes no ports: it only makes outbound calls.
MY_IP="$(curl -fsS https://api.ipify.org)"
echo "==> Creating SSH firewall rule for $MY_IP"
if gcloud compute firewall-rules describe allow-ssh-bot --quiet >/dev/null 2>&1; then
  echo "    rule already exists, updating source range"
  gcloud compute firewall-rules update allow-ssh-bot --source-ranges="$MY_IP" --quiet
else
  gcloud compute firewall-rules create allow-ssh-bot \
    --allow=tcp:22 \
    --source-ranges="$MY_IP" \
    --network=default \
    --target-tags=bot \
    --quiet
fi

# A fresh project ships default-allow-ssh and default-allow-rdp, which open
# port 22 (and 3389) to 0.0.0.0/0. The rule above is the only SSH we need, so
# delete the wide-open ones rather than leaving a public SSH door behind.
for open_rule in default-allow-ssh default-allow-rdp; do
  if gcloud compute firewall-rules describe "$open_rule" --quiet >/dev/null 2>&1; then
    echo "==> Removing wide-open rule $open_rule"
    gcloud compute firewall-rules delete "$open_rule" --quiet
  fi
done

# --- VM ----------------------------------------------------------------
if gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --quiet >/dev/null 2>&1; then
  echo "==> VM $VM_NAME already exists, reusing it"
else
  echo "==> Creating $VM_NAME (e2-micro, 30 GB)"
  # No Cloud NAT: an instance with an external IP gets outbound internet
  # directly. A NAT Gateway would cost roughly $32/mo and is not needed.
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" \
    --machine-type=e2-micro \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --tags=bot \
    --scopes=logging-write \
    --boot-disk-size=30GB \
    --boot-disk-type=pd-standard \
    --quiet
fi

IP="$(gcloud compute instances describe "$VM_NAME" \
  --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"

cat <<INFO

VM ready: $VM_NAME at $IP

Next steps, from this machine:

  ./deploy/deploy.sh $SSH_USER@$IP
  ssh $SSH_USER@$IP
  ./deploy/vm-setup.sh
  cd ~/tezos-offers-bot
  cp .env.example .env
  # set TELEGRAM_BOT_TOKEN, then append:
  #   echo "POSTGRES_PASSWORD=\$(openssl rand -hex 16)" >> .env
  docker compose up -d --build
  docker compose logs -f bot

INFO

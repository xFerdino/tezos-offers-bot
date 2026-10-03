#!/usr/bin/env bash
#
# Provision an AWS Lightsail instance for the bot.
#
# Why Lightsail and not EC2:
#   - Flat bundle: compute + disk + static public IPv4 + data transfer, so no
#     per-component surprises on top of the bundle.
#   - Managed networking avoids provisioning an EC2 NAT Gateway, which the
#     bot never needs.
#   - Public IPv4 supports outbound calls to Telegram and the NFT indexers.
#     Telegram updates are polled; no inbound application port is needed.
#
# Size: micro_3_0 gives 1 GB RAM, 40 GB disk and 2 TB transfer. nano_3_0 only
# has 0.5 GB RAM, which is too tight for Postgres.
#
# Usage:
#   export AWS_REGION=us-east-1     # optional, this is the default
#   ./deploy/aws-setup.sh
#
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
INSTANCE_NAME="${INSTANCE_NAME:-tezos-offers-bot}"
# RAM size in GB. 1.0 = micro_3_0. 2.0 = small_3_0.
RAM_GB="${RAM_GB:-1.0}"

if ! command -v aws >/dev/null 2>&1; then
  cat >&2 <<'EOF'
error: the AWS CLI is not installed. Install it with:

  curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
  sudo apt-get install -y unzip
  unzip awscliv2.zip
  sudo ./aws/install
  aws --version

Then run this script again.
EOF
  exit 1
fi

echo "==> Region: $REGION   Instance: $INSTANCE_NAME"

# --- Check for an existing instance first (re-runs are safe) -------------
EXISTING="$(aws lightsail get-instances --region "$REGION" \
  --query "data[?name=='$INSTANCE_NAME'].publicIpAddress | [0]" \
  --output text 2>/dev/null || echo "")"

if [[ -n "$EXISTING" && "$EXISTING" != "None" && "$EXISTING" != "" ]]; then
  echo "==> $INSTANCE_NAME already exists at $EXISTING - reusing it"
  cat <<INFO

Next steps, from this machine:

  ./deploy/deploy.sh ubuntu@$EXISTING
  ssh ubuntu@$EXISTING
  ./deploy/vm-setup.sh
  cd ~/tezos-offers-bot
  cp .env.example .env
  # set TELEGRAM_BOT_TOKEN, then:
  #   echo "POSTGRES_PASSWORD=\$(openssl rand -hex 16)" >> .env
  docker compose up -d --build
  docker compose logs -f bot

INFO
  exit 0
fi

# --- Look up the bundle id for the requested RAM size -------------------
# Bundle ids are not human-readable (micro_3_0, small_3_0, ...), so resolve
# them from the API rather than hardcoding.
echo "==> Looking up a ${RAM_GB} GB bundle"
BUNDLE_ID="$(aws lightsail get-bundles --region "$REGION" \
  --query "bundles[?isActive==\`true\` && ramSizeInGb==\`$RAM_GB\` && publicIpv4AddressCount==\`1\`].bundleId | [0]" \
  --output text 2>/dev/null || echo "None")"

if [[ -z "$BUNDLE_ID" || "$BUNDLE_ID" == "None" ]]; then
  echo "error: no ${RAM_GB} GB bundle with a public IPv4 found in $REGION" >&2
  echo "available options:" >&2
  aws lightsail get-bundles --region "$REGION" \
    --query 'bundles[?isActive==\`true\`].{id:bundleId,ram:ramSizeInGb,ipv4:publicIpv4AddressCount,price:price}' \
    --output table >&2
  exit 1
fi
BUNDLE_PRICE="$(aws lightsail get-bundles --region "$REGION" \
  --query "bundles[?bundleId=='$BUNDLE_ID'].price | [0]" --output text)"
echo "    bundle: $BUNDLE_ID  (\$$BUNDLE_PRICE/mo)"

# --- Look up the newest Ubuntu 24.04 blueprint -------------------------
BLUEPRINT_ID="$(aws lightsail get-blueprints --region "$REGION" \
  --query "sort_by(blueprints[?isPublic==\`true\` && type==\`os\` && platform==\`LINUX_UNNX\` && name=='Ubuntu'], -version)[0].blueprintId" \
  --output text 2>/dev/null || echo "None")"

if [[ -z "$BLUEPRINT_ID" || "$BLUEPRINT_ID" == "None" ]]; then
  echo "error: no Ubuntu blueprint found in $REGION" >&2
  exit 1
fi
echo "    blueprint: $BLUEPRINT_ID"

# --- Which key pair can we SSH with? ------------------------------------
KEY_PAIR="$(aws lightsail get-instance-ssh-key-name --region "$REGION" --output text 2>/dev/null || echo "")"
if [[ -n "$KEY_PAIR" ]]; then
  echo "    key pair: $KEY_PAIR"
else
  echo "    key pair: (none yet - the default key pair is created automatically)"
fi

# --- Create ------------------------------------------------------------
echo "==> Creating $INSTANCE_NAME"
aws lightsail create-instances \
  --region "$REGION" \
  --instance-names "$INSTANCE_NAME" \
  --availability-zone "${REGION}a" \
  --blueprint-id "$BLUEPRINT_ID" \
  --bundle-id "$BUNDLE_ID" \
  --quiet

echo "==> Waiting for boot (2-5 minutes)"
aws lightsail wait instance-running --region "$REGION" --instance-names "$INSTANCE_NAME"

# The public IP is only populated once the instance is fully running.
for _ in $(seq 1 30); do
  PUBLIC_IP="$(aws lightsail get-instances --region "$REGION" \
    --query "data[?name=='$INSTANCE_NAME'].publicIpAddress | [0]" \
    --output text 2>/dev/null || echo "None")"
  [[ -n "$PUBLIC_IP" && "$PUBLIC_IP" != "None" ]] && break
  sleep 5
done

cat <<INFO

Lightsail instance ready
  name:   $INSTANCE_NAME
  ip:     $PUBLIC_IP
  user:   ubuntu
  bundle: $BUNDLE_ID (\$$BUNDLE_PRICE/mo)

Next steps, from this machine:

  ./deploy/deploy.sh ubuntu@$PUBLIC_IP
  ssh ubuntu@$PUBLIC_IP
  ./deploy/vm-setup.sh
  cd ~/tezos-offers-bot
  cp .env.example .env
  # set TELEGRAM_BOT_TOKEN, then:
  #   echo "POSTGRES_PASSWORD=\$(openssl rand -hex 16)" >> .env
  docker compose up -d --build
  docker compose logs -f bot

INFO

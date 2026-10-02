# Tezos Offers Bot

A Telegram bot that watches the NFTs in your Tezos wallet and tells you the
moment someone makes an offer on one. Built to match the core of
[cryptonoises.com](https://cryptonoises.com), focused on the feature that
matters most: **offers on the NFTs you already hold**.

## Supported marketplaces

| Marketplace | Source | Notes |
|---|---|---|
| **objkt.com** | objkt v3 GraphQL | Also covers v1/v6 contract generations |
| **fxhash** | objkt v3 GraphQL | Its own API is Cloudflare-blocked for servers |
| **hic et nunc** | objkt v3 GraphQL | Legacy HEN marketplace contracts |
| **akaSwap** | objkt v3 GraphQL | |
| **Teia** | teztok.teia.rocks | Separate indexer; objkt does not index Teia offers |
| Versum | not supported | Platform shut down 2023-12-01, contract frozen |

objkt's indexer aggregates offers made on fxhash, HEN and akaSwap, so a single
paginated wallet query covers all of them. It also includes objkt project
offers on fxhash collections held by the wallet. Teia requires its own pass.

## Setup

### 1. Create a bot token

Message [@BotFather](https://t.me/BotFather) and use `/newbot`.

Use a **new** token. If another app already uses your existing token, both
apps will poll `getUpdates` on it and steal each other's updates.

### 2. Install (local development)

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
cp .env.example .env
```

### 3. Deploy to a server

See [Deployment](#deployment) below for the recommended GCP setup.

## Deployment

**It runs on Google Cloud at $0/month, indefinitely.** This is not a theory:
the bot is deployed and running on a GCP `e2-micro` in the Always Free tier,
tracking a 3,102-NFT wallet. Everything below is the setup that was actually
used, not a hypothetical.

### Why it is free

| Resource | Cost | Note |
|---|---|---|
| `e2-micro` VM | **$0** | The only Always Free instance type. 1 GB RAM, 30 GB disk. One per project, free forever |
| Boot disk | **$0** | Must be **standard PD**. The 30 GB free-tier allowance does *not* cover `balanced` or `SSD` PD, which bill from the first byte |
| Compute Engine API | $0 | Enabling an API does not bill |
| Egress | $0 | 1 GB/month free from North America; image previews use a fraction of that |
| Cloud NAT Gateway | **~$32/mo** | **Do not create one.** Not needed — the VM's external IP handles all outbound traffic |
| Static IP | $0 | In-use addresses are free; only *reserved* addresses cost |

Billing must be enabled on the project, and the VM must be in `us-west1`,
`us-central1` or `us-east1` to qualify.

The disk type is the easy mistake to make: `e2-micro` is free but a
`pd-balanced` boot disk is not, and the instance then bills every second of
the month. `deploy/gcp-setup.sh` passes `--boot-disk-type=pd-standard` for
this reason. To convert an existing VM, snapshot it, create a `pd-standard`
disk from the snapshot, and recreate the instance against that disk.

### Two traps that cost real money

Both of these are silent. Nothing errors, the VM runs fine, and the bill
arrives later.

**1. `pd-balanced` boot disk.** A free instance type does not make the setup
free. The 30 GB Always Free allowance applies to **standard PD only** - the
e2-micro stays free, the disk does not. Balanced and SSD PD bill from the
first byte, and the disk is what actually charges you.

**2. Default firewall rules.** Every new GCP project ships with
`default-allow-ssh` and `default-allow-rdp`, which open ports 22 and 3389 to
`0.0.0.0/0`. This bot needs no inbound traffic at all - it only makes
outbound calls. A key-only VM tolerates it, but a weak or reused password
does not, and a public port 22 is an invitation. `deploy/gcp-setup.sh` creates
an IP-scoped SSH rule and then deletes both default rules.

Check your own project any time:

```bash
gcloud compute disks list          # TYPE must read pd-standard
gcloud compute firewall-rules list # no rule may allow 0.0.0.0/0 on tcp:22
```

Target: **GCP `e2-micro`** — the only instance type in the Always Free tier,
1 GB RAM and 30 GB disk, **free permanently** (not free for 6 months like the
AWS credit). Available in `us-west1`, `us-central1` and `us-east1`.

```bash
# 1. install the Google Cloud CLI
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
  | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt-get update && sudo apt-get install -y google-cloud-cli

# 2. sign in (opens a browser)
gcloud auth login

# 3. create the VM
export GCP_PROJECT_ID=<your-project-id>
./deploy/gcp-setup.sh
```

The script enables the Compute API, creates an SSH firewall rule scoped to
**your IP only**, and launches the e2-micro. It prints the IP when done.
Re-running is safe: an existing VM is reused rather than duplicated.

If the project is not yet linked to a billing account, link it first:

```bash
gcloud billing projects link <your-project-id> --billing-account=<billing-id>
```

```bash
gcloud compute instances list --project=<your-project-id>
gcloud compute routers list --project=<your-project-id>   # should be empty
```

Then deploy:

```bash
./deploy/deploy.sh <user>@THE_IP     # from this machine
ssh <user>@THE_IP
./deploy/vm-setup.sh                 # installs Docker on the VM
cd ~/tezos-offers-bot
cp .env.example .env                 # set TELEGRAM_BOT_TOKEN and POSTGRES_PASSWORD
docker compose up -d --build
docker compose logs -f bot
```

`docker-compose.yml` runs Postgres alongside the bot, tuned for 1 GB of RAM
(`shared_buffers=64MB`, `max_connections=20`). The bot needs no published
port: it only makes outbound calls.

**Cost: $0/month, indefinitely.** e2-micro is free in the Always Free tier.
Two things to avoid:

- Do **not** create a Cloud NAT Gateway — roughly $32/mo, which would turn a
  free instance into a paid one. The VM's own external IP handles outbound
  traffic, so nothing is needed.
- The bot sends images over Telegram, so it consumes a small amount of egress.
  The free tier allows 1 GB/month from North America, which is ample here.

> **Run exactly one instance per bot token.** Two processes polling the same
> token with `getUpdates` terminate each other, and Telegram reports
> `Conflict: terminated by other getUpdates request`. If you run the bot
> locally while it is deployed, stop the local copy first.

> **Only one bot instance may be running at a time**, and a wallet with
> thousands of NFTs is expensive to scan — see
> [Scan cost](#scan-cost) before dropping the interval to 60 seconds.

### Alternative: AWS Lightsail

If you would rather use the $100 AWS credit, `deploy/aws-setup.sh` creates a
Lightsail instance. `micro_3_0` is $7/mo (1 GB, 40 GB disk, 2 TB transfer),
so roughly $42 over six months. Lightsail is a flat bundle with a static IPv4
included and no VPC, which avoids the ~$32.85/mo NAT Gateway trap. The AWS
free tier no longer includes free compute hours, so this is credit-funded
rather than free.

## Commands

| Command | What it does |
|---|---|
| `/start` | Intro and help |
| `/track tz1…` | Start watching a wallet (or just send the address) |
| `/untrack tz1…` | Stop watching a wallet |
| `/wallet` | Your tracked wallets and NFT counts |
| `/offers` | All active offers, highest first, with a total |
| `/min 5` | Only alert me for offers of 5 XTZ or more (`/min` to show, `/min 0` for all) |
| `/scan` | Force an immediate scan |

## How it works

```
every SCAN_INTERVAL seconds (default 300):
  for each tracked wallet:
    1. resolve held NFTs           objkt: token_holder
    2. fetch active offers         objkt: offer_active (wallet + owned projects)
                                   teia: offers (batched by contract + token_id)
    3. diff against stored offers  only new (marketplace, offer_id) pairs
    4. convert XTZ -> USD          TzKT /v1/head quoteUsd
    5. send an alert per new offer
```

Offers are deduplicated on `(telegram_id, marketplace, offer_id)`, so each
offer normally alerts once. Failed Telegram deliveries stay pending in
Postgres and retry on a later scan; failed image previews fall back to text.
Offers that disappear from the indexer are marked `expired` rather than
deleted, keeping history intact. A crash between sending and acknowledging
an alert can still cause a duplicate.

Project offers have no individual `token_pk`. The bot matches their
`collection_offer` value (`agpk:<gallery pk>`) to the projects of NFTs you
hold, and sends one alert per offer even if you own several matching
editions. The alert includes a link to an eligible NFT on objkt.

### Alert threshold

`/min 5` sets the smallest offer you want to hear about, in XTZ. It is a
notification preference, not a tracking change:

```bash
/min        # show the current threshold
/min 2.5    # only alert for offers of 2.5 XTZ or more
/min 0      # back to alerting on everything (the default)
```

An offer exactly at the threshold alerts. Offers below it are still stored and
still counted in `/offers`, and `/scan` reports how many it held back, so
nothing is hidden — you just stop being pinged for dust bids.

### Scan cost

Teia is polled on its own slower cadence (`TEIA_SCAN_INTERVAL`, default 600s)
because it costs one query per held token. objkt filters ordinary offers
by wallet ownership on the server and includes matching project offers,
with 100 offers per page.

Measured on a real wallet on 2026-09-30 (3,062 positive NFT balances):

| Stage | Requests | Time |
|---|---|---|
| Holdings and project memberships (`token_holder`, 100/page) | 31 | 18s |
| objkt token + project offers (488 offers, 100/page) | 5 | 27s |
| Teia offers (per token, previous measurement) | ~3100 | **254s** |

This objkt pass took about **45 seconds**. The loop waits `SCAN_INTERVAL`
after each complete cycle, so `60` means a 60-second pause, not a guaranteed
one-minute alert latency. A Teia pass and additional wallets add time.

The wallet filter is `token.holders.holder_address` with `quantity > 0`.
`target_address` and `seller_address` alone do not identify the owner of an
ordinary open offer. Holdings are still fetched in pages for project
membership, NFT counts, and the separate Teia scan.

Teia is not a subset of objkt. On the 3,102-NFT wallet, 93 tokens carried a
Teia offer and only 8 of those also had an objkt offer, so skipping Teia
entirely would miss 85 tokens. On the 8 shared tokens Teia was the better bid
3 times. That is why Teia is slowed down rather than removed.

### API details worth knowing

- Project offers deliberately have `token_pk: null` and `token: null`;
  this is not evidence of an indexing delay. Match their gallery IDs instead.
- `token_pk` is a `bigint` in the schema, so it must be passed as a string.
- Comparison operators are underscore-prefixed: `_eq`, `_in`.
- Teia must be filtered by **both** `fa2_address` and `token_id`. Filtering by
  contract alone returns arbitrary recent offers for that contract.
- Teia rows can have a null `offer_id`; those are collection-level offers and
  are skipped, since they cannot be addressed to a token.

## Tests

```bash
.venv/bin/python tests/test_pipeline.py   # live mainnet scan, needs network
.venv/bin/python tests/test_db.py         # real PostgreSQL: schema, dedupe, expiry
.venv/bin/python tests/test_startup.py    # DB connect + Application wiring
.venv/bin/python tests/test_offer_delivery.py # wallet/project matching + Telegram payloads
```

`test_pipeline.py` runs a full live scan against a wallet that holds NFTs and
asserts dedupe behaviour across two consecutive scans. `test_db.py` and
`test_startup.py` spin up an embedded PostgreSQL via `pgserver`, so they need
no external database. All three require network access except where noted.

`deploy/gcp-setup.sh` provisions the always-free `e2-micro` and is the primary
target; `deploy/aws-setup.sh` is available as a paid alternative.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Required |
| `POSTGRES_PASSWORD` | — | Required under Docker Compose |
| `SCAN_INTERVAL` | `300` | Seconds between scans |
| `TEIA_SCAN_INTERVAL` | `600` | Seconds between Teia passes; see [Scan cost](#scan-cost) |
| `OBJKT_RATE_LIMIT_RPM` | `100` | Client-side throttle (objkt allows 120) |
| `DEBUG` | `false` | Verbose logging |
| `DATABASE_URL` | set by compose | Only for running outside Docker |

## Not implemented

Deliberately out of scope for this version, listed so the gaps are explicit:
sales/purchases, mints, listings, royalties, auctions, .tez domain expiry,
coin-rate conversions, Discord support, and premium tiers.

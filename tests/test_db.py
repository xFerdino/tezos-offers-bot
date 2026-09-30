"""Validate the schema and every Database query against a real PostgreSQL.

Starts an embedded Postgres via pgserver, applies the schema, and exercises
upsert dedupe, expiry, and the dashboard queries.

Run: .venv/bin/python tests/test_db.py
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import shutil
import sys

import pgserver

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bot.db import Database  # noqa: E402
from bot.models import Offer  # noqa: E402

logging.basicConfig(level=logging.WARNING)

TELEGRAM_ID = 42
ADDRESS = "tz1UBZUkXpKGhYsP5KtzDNqLLchwF4uHrGjw"


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return not condition


def make_offers(n: int, marketplace: str = "objkt") -> list[Offer]:
    return [
        Offer(
            marketplace=marketplace,
            offer_id=str(1000 + i),
            token_pk="22356",
            token_id=str(154 + i),
            contract="KT1RJ6PbjHpwc3M5rw5s2Nbmefwbuwbdxton",
            token_name=f"Token {i}",
            media_uri="ipfs://QmTest",
            price_mutez=1_000_000 * (i + 1),
            buyer="tz1buyer",
        )
        for i in range(n)
    ]


async def run(dsn: str) -> int:
    failures = 0
    db = await Database.connect(dsn)

    print("\n== schema ==")
    rows = await db._pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    )
    tables = {r["table_name"] for r in rows}
    failures += check("wallets table created", "wallets" in tables, str(sorted(tables)))
    failures += check("offers table created", "offers" in tables)

    print("\n== schema is idempotent ==")
    await db.init_schema()
    failures += check("re-running init_schema is safe", True)

    print("\n== wallets ==")
    await db.add_wallet(TELEGRAM_ID, ADDRESS, "my main")
    await db.add_wallet(TELEGRAM_ID, ADDRESS, "renamed")
    wallets = await db.list_wallets(TELEGRAM_ID)
    failures += check("wallet stored once", len(wallets) == 1, f"{len(wallets)} rows")
    failures += check("label updated on re-add", wallets[0]["label"] == "renamed", wallets[0]["label"])

    await db.mark_scanned(TELEGRAM_ID, ADDRESS, 151)
    wallets = await db.list_wallets(TELEGRAM_ID)
    failures += check("nft_count saved", wallets[0]["nft_count"] == 151, str(wallets[0]["nft_count"]))
    failures += check("last_scanned set", wallets[0]["last_scanned_at"] is not None)

    failures += check("all_wallets returns row", len(await db.all_wallets()) == 1)

    print("\n== offers: insert + dedupe ==")
    offers = make_offers(5)
    new = await db.upsert_offers(TELEGRAM_ID, ADDRESS, offers, usd_rate=0.30)
    failures += check("first upsert returns all new", len(new) == 5, f"{len(new)} new")

    new2 = await db.upsert_offers(TELEGRAM_ID, ADDRESS, offers, usd_rate=0.30)
    failures += check("second upsert returns none new (dedupe)", len(new2) == 0, f"{len(new2)} new")

    stats = await db.offer_stats(TELEGRAM_ID)
    failures += check("stats count = 5", stats["total"] == 5, str(stats["total"]))
    failures += check("stats sum mutez", stats["total_mutez"] == sum(o.price_mutez for o in offers), str(stats["total_mutez"]))
    failures += check("stats wallets = 1", stats["wallets"] == 1)

    print("\n== offers: usd conversion ==")
    listed = await db.list_offers(TELEGRAM_ID)
    top = listed[0]
    failures += check("highest price first", top["price_mutez"] == 5_000_000, str(top["price_mutez"]))
    expected_usd = round(5_000_000 / 1e6 * 0.30, 2)
    failures += check("usd stored", abs(top["price_usd"] - expected_usd) < 0.01, str(top["price_usd"]))
    failures += check("token name stored", top["token_name"] == "Token 4", str(top["token_name"]))

    print("\n== offers: no usd rate ==")
    await db.upsert_offers(TELEGRAM_ID, ADDRESS, make_offers(2, "teia"), usd_rate=None)
    teia_rows = await db._pool.fetch(
        "SELECT price_usd FROM offers WHERE marketplace='teia' LIMIT 1"
    )
    failures += check("usd is null without rate", teia_rows[0]["price_usd"] is None, str(teia_rows[0]["price_usd"]))

    print("\n== offers: expiry scoped to marketplace ==")
    # Only objkt offers survive; teia offers must be untouched.
    active = {("objkt", "1000")}
    expired = await db.mark_expired(
        TELEGRAM_ID, ADDRESS, active, marketplaces=["objkt"]
    )
    failures += check("objkt offers expired", expired > 0, f"{expired} expired")

    teia_after = await db._pool.fetch(
        "SELECT status FROM offers WHERE marketplace='teia' AND status='active'"
    )
    failures += check(
        "teia offers NOT expired by objkt sweep",
        len(teia_after) == 2,
        f"{len(teia_after)} still active",
    )

    remaining = await db._pool.fetch(
        "SELECT marketplace, offer_id FROM offers WHERE telegram_id=$1 AND status='active'",
        TELEGRAM_ID,
    )
    remaining_keys = {(r["marketplace"], r["offer_id"]) for r in remaining}
    failures += check(
        "only intended objkt offer remains active",
        ("objkt", "1000") in remaining_keys,
        str(sorted(remaining_keys))[:120],
    )

    print("\n== offers: re-adding an expired offer alerts again ==")
    reintroduced = [
        Offer(
            marketplace="objkt",
            offer_id="1001",
            token_pk="22356",
            token_id="155",
            contract="KT1RJ6PbjHpwc3M5rw5s2Nbmefwbuwbdxton",
            token_name="Token 1",
            media_uri=None,
            price_mutez=2_000_000,
            buyer="tz1buyer",
        )
    ]
    new3 = await db.upsert_offers(TELEGRAM_ID, ADDRESS, reintroduced, 0.30)
    failures += check("re-added expired offer is new again", len(new3) == 1, f"{len(new3)}")
    status_row = await db._pool.fetchrow(
        "SELECT status FROM offers WHERE marketplace='objkt' AND offer_id='1001'"
    )
    failures += check("re-added offer is active", status_row["status"] == "active", status_row["status"])

    print("\n== offers: still-active offer is NOT re-alerted ==")
    # offer 1000 is still active; re-offering it must produce no alert.
    still_active = [
        Offer(
            marketplace="objkt",
            offer_id="1000",
            token_pk="22356",
            token_id="154",
            contract="KT1RJ6PbjHpwc3M5rw5s2Nbmefwbuwbdxton",
            token_name="Token 0",
            media_uri=None,
            price_mutez=1_000_000,
            buyer="tz1buyer",
        )
    ]
    new4 = await db.upsert_offers(TELEGRAM_ID, ADDRESS, still_active, 0.30)
    failures += check("no duplicate alert for active offer", len(new4) == 0, f"{len(new4)}")

    # Offers 1002-1004 were expired, so re-alerting them is correct.
    revived = await db.upsert_offers(TELEGRAM_ID, ADDRESS, make_offers(5), 0.30)
    failures += check(
        "previously expired offers re-alert", len(revived) == 3, f"{len(revived)} revived"
    )

    print("\n== isolation between users ==")
    other_new = await db.upsert_offers(999, ADDRESS, make_offers(3), 0.30)
    failures += check("different telegram_id sees all as new", len(other_new) == 3, f"{len(other_new)}")

    print("\n== untrack ==")
    print("\n== expiry preserves exact marketplace/offer pairs ==")
    uid = 1001
    await db.upsert_offers(uid, ADDRESS, make_offers(3) + make_offers(3, "fxhash"), None)
    active = {("objkt", "1000"), ("objkt", "1001"), ("fxhash", "1002")}
    await db.mark_expired(uid, ADDRESS, active, marketplaces=["objkt", "fxhash"])
    failures += check("all live pairs survive", await db.known_offer_keys(uid, ADDRESS) == active)
    await db.mark_expired(uid, ADDRESS, set(), marketplaces=["objkt"])
    failures += check(
        "empty successful scan expires only its source",
        await db.known_offer_keys(uid, ADDRESS) == {("fxhash", "1002")},
    )

    print("\n== delivery retries after Telegram failure ==")
    from bot import main as bot_main
    from bot.scanner import WalletScan

    failures += check("pending delivery path exists", hasattr(bot_main, "notify_pending_offers"))
    if hasattr(bot_main, "notify_pending_offers"):
        sent = []

        class Telegram:
            failing = True

            async def send_photo(self, *, chat_id, photo, caption, parse_mode):
                raise RuntimeError("image unavailable")

            async def send_message(self, *, chat_id, text, **kwargs):
                if self.failing:
                    raise RuntimeError("Telegram unavailable")
                sent.append(text)

        class Prices:
            async def xtz_to_usd(self):
                return None

        telegram = Telegram()
        uid = 1002
        await db.upsert_offers(uid, ADDRESS, make_offers(2), None)
        await db.set_min_alert_mutez(uid, 2_000_000)
        scan = WalletScan(uid, ADDRESS, 2, [], 0)
        await bot_main.notify_pending_offers(scan, telegram, Prices(), db)
        failures += check("failed delivery remains pending", len(await db.pending_offers(uid, ADDRESS)) == 1)
        telegram.failing = False
        await bot_main.notify_pending_offers(scan, telegram, Prices(), db)
        await bot_main.notify_pending_offers(scan, telegram, Prices(), db)
        failures += check("retry delivers once and respects threshold", len(sent) == 1 and "2 XTZ" in sent[0])
        failures += check("successful delivery is acknowledged", not await db.pending_offers(uid, ADDRESS))

    removed = await db.remove_wallet(TELEGRAM_ID, ADDRESS)
    failures += check("wallet removed", removed)
    failures += check("second remove returns false", not await db.remove_wallet(TELEGRAM_ID, ADDRESS))

    await db.close()
    return failures


async def main() -> int:
    # Fresh cluster per run: the data dir persists between runs, and these
    # tests assert on insert counts, so leftover rows would skew results.
    data_dir = pathlib.Path("/tmp/pgdata_bot_test")
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(exist_ok=True)

    server = pgserver.get_server(data_dir)
    # get_uri() is the portable DSN: a unix socket on POSIX, TCP on Windows.
    dsn = server.get_uri()
    print(f"dsn: {dsn}")

    failures = await run(dsn)

    print("\n" + "=" * 46)
    if failures:
        print(f"RESULT: {failures} check(s) FAILED")
        return 1
    print("RESULT: all checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""End-to-end pipeline check against the live APIs, without a database.

Run: .venv/bin/python tests/test_pipeline.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.alerts import build_alert, build_summary  # noqa: E402
from bot.scanner import Scanner  # noqa: E402
from bot.sources.objkt import ObjktClient  # noqa: E402
from bot.sources.teia import TeiaClient  # noqa: E402
from bot.sources.tzkt import TzktPriceClient  # noqa: E402
from bot.utils import (  # noqa: E402
    format_xtz,
    is_valid_tezos_address,
    resolve_media_uri,
    shorten_address,
)

logging.basicConfig(level=logging.WARNING)

WALLET = "tz1UBZUkXpKGhYsP5KtzDNqLLchwF4uHrGjw"
TELEGRAM_ID = 123456789


class FakeDB:
    """In-memory Database stand-in mirroring the dedupe semantics."""

    def __init__(self) -> None:
        self.wallets = {}
        self.offers = {}

    async def add_wallet(self, telegram_id, address, label=None):
        self.wallets[(telegram_id, address)] = 0

    async def mark_scanned(self, telegram_id, address, nft_count):
        self.wallets[(telegram_id, address)] = nft_count

    async def upsert_offers(self, telegram_id, address, offers, usd_rate):
        new = []
        for o in offers:
            key = f"{telegram_id}|{address}|{o.marketplace}:{o.offer_id}"
            if key not in self.offers:
                self.offers[key] = {"price_mutez": o.price_mutez}
                new.append(o)
        return new

    async def mark_expired(self, telegram_id, address, active_keys, marketplaces=None):
        return 0


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return not condition


async def main() -> int:
    failures = 0

    print("\n== unit checks ==")
    failures += check("valid tz address", is_valid_tezos_address(WALLET))
    failures += check("rejects short address", not is_valid_tezos_address("tz1abc"))
    failures += check("rejects garbage", not is_valid_tezos_address("hello world"))
    failures += check("mutez -> XTZ", format_xtz(10_000_000) == "10", format_xtz(10_000_000))
    failures += check("trims zeros", format_xtz(1_500_000) == "1.5", format_xtz(1_500_000))
    failures += check("small amount", format_xtz(100_000) == "0.1", format_xtz(100_000))
    failures += check("zero", format_xtz(0) == "0")
    failures += check("shorten", shorten_address(WALLET) == "tz1UBZ...rGjw", shorten_address(WALLET))
    failures += check("ipfs resolved", "ipfs" in (resolve_media_uri("ipfs://Qm123") or ""))
    failures += check("http passthrough", resolve_media_uri("https://x.test/a.png") == "https://x.test/a.png")
    failures += check("garbage uri -> None", resolve_media_uri("nope:") is None)

    async with httpx.AsyncClient(timeout=45.0) as http:
        objkt = ObjktClient(http, rate_limit_rpm=100)
        teia = TeiaClient(http)
        prices = TzktPriceClient(http)

        print("\n== live: holdings ==")
        holdings = await objkt.get_holdings(WALLET)
        failures += check("holdings found", len(holdings) > 0, f"{len(holdings)} NFTs")
        if holdings:
            failures += check("token_pk numeric", holdings[0].token_pk.isdigit(), holdings[0].token_pk)
            failures += check("contract is KT1", holdings[0].contract.startswith("KT1"), holdings[0].contract)

        print("\n== live: offers via objkt indexer ==")
        offers = await objkt.get_offers_for_wallet(WALLET, holdings)
        failures += check("offers found", len(offers) > 0, f"{len(offers)} offers")
        markets = {o.marketplace for o in offers}
        failures += check("no unknown marketplace", "unknown" not in markets, str(markets))
        failures += check("all priced", all(o.price_mutez > 0 for o in offers))
        failures += check(
            "tokens match holdings",
            all(o.token_pk in {h.token_pk for h in holdings} for o in offers if o.token_pk),
        )

        print("\n== live: teia offers ==")
        teia_offers = await teia.get_offers_for_holdings(holdings)
        failures += check("teia query works", isinstance(teia_offers, list), f"{len(teia_offers)} rows")
        failures += check(
            "teia offers are on held tokens",
            all((o.contract, o.token_id) in {(h.contract, h.token_id) for h in holdings} for o in teia_offers),
            f"{len(teia_offers)} for {len(holdings)} held",
        )
        failures += check(
            "teia rows have offer ids",
            all(o.offer_id and o.offer_id != "None" for o in teia_offers),
        )
        failures += check("teia priced", all(o.price_mutez > 0 for o in teia_offers))

        print("\n== live: price ==")
        usd = await prices.xtz_to_usd()
        failures += check("usd rate", bool(usd and usd > 0), f"1 XTZ = ${usd}")

        print("\n== scanner dedupe ==")
        db = FakeDB()
        scanner = Scanner(db, objkt, teia, prices)

        first = await scanner.scan_wallet(TELEGRAM_ID, WALLET)
        failures += check("first scan no error", first.error is None, str(first.error or ""))
        failures += check("first scan found offers", len(first.new_offers) > 0, f"{len(first.new_offers)} new")
        failures += check("nft count recorded", first.nft_count == len(holdings), str(first.nft_count))

        second = await scanner.scan_wallet(TELEGRAM_ID, WALLET)
        failures += check(
            "second scan zero new (dedupe)",
            len(second.new_offers) == 0,
            f"{len(second.new_offers)} new",
        )

        print("\n== teia context attachment ==")
        matched = Scanner._attach_teia_context(teia_offers, holdings)
        failures += check(
            "teia filtered to held tokens",
            all((o.contract, o.token_id) in {(h.contract, h.token_id) for h in holdings} for o in matched),
            f"{len(matched)}/{len(teia_offers)} matched",
        )
        failures += check("matched teia have names", all(o.token_name for o in matched))

        print("\n== alert formatting ==")
        if first.new_offers:
            o = first.new_offers[0]
            msg = build_alert(o, usd)
            failures += check("has text", bool(msg.get("text")))
            failures += check("html mode", msg.get("parse_mode") == "HTML")
            failures += check("shows XTZ", "XTZ" in msg["text"], msg["text"].splitlines()[1])
            failures += check("shows usd", "$" in msg["text"])
            print("\n--- sample alert ---")
            print(msg["text"])

        rows = [
            {
                "marketplace": o.marketplace,
                "price_mutez": o.price_mutez,
                "price_usd": round(o.price_mutez / 1e6 * usd, 2) if usd else None,
                "token_name": o.token_name,
                "token_id": o.token_id,
            }
            for o in first.new_offers
        ]
        summary = build_summary(rows, usd)
        failures += check("summary non-empty", bool(summary))
        failures += check("summary has total", "Total:" in summary)
        print("\n--- sample summary (900 chars) ---")
        print(summary[:900])

        print("\n== empty state ==")
        failures += check("empty summary ok", "No active offers" in build_summary([], usd))

    print("\n" + "=" * 46)
    if failures:
        print(f"RESULT: {failures} check(s) FAILED")
        return 1
    print("RESULT: all checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

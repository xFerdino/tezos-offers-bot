"""Verify the bot's real startup path: connect to Postgres, create schema.

Exercises Database.connect(), init_schema() and Application wiring against a
real cluster. Does not need a Telegram token, so it can run before the bot is
registered.

Run: .venv/bin/python tests/test_startup.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import shutil
import sys
import tempfile

import httpx
import pgserver

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bot.db import Database  # noqa: E402
from bot.main import build_application, notify_new_offers  # noqa: E402
from bot.scanner import Scanner  # noqa: E402
from bot.sources.objkt import ObjktClient  # noqa: E402
from bot.sources.teia import TeiaClient  # noqa: E402
from bot.sources.tzkt import TzktPriceClient  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return not condition


def check_expiry_scope():
    """Regression: expiry scope was derived from the offers seen this scan.

    A marketplace with zero live offers then dropped out of scope and its
    stored offers could never be marked expired. Scope must be the fixed set of
    labels we poll, and an unmapped objkt contract must not become "unknown".
    """
    print("\n== Marketplace labelling and expiry scope ==")
    failures = 0

    from bot.sources.objkt import GROUP_LABELS, ObjktClient
    from bot.scanner import obkt_expiry_scope

    scope = obkt_expiry_scope()
    failures += check(
        "expiry scope covers every label we can label",
        set(GROUP_LABELS.values()) <= set(scope),
        str(scope),
    )
    failures += check(
        "expiry scope has no 'unknown' bucket",
        "unknown" not in scope,
        str(scope),
    )

    # Two real contracts absent from the hardcoded map, verified live.
    for contract, group, want in [
        ("KT1J2C7BsYNnSjQsGoyrSXShhYGkrDDLVGDd", "akaswap", "akaSwap"),
        ("KT19NMJYBC4AiprtetMW1K2damXgGRCbJWD5", "objktcom", "objkt"),
    ]:
        offer = ObjktClient._parse_offer(
            {
                "id": 1,
                "token_pk": 1,
                "marketplace_contract": contract,
                "marketplace": {"name": "x", "group": group},
                "token": {"token_id": "1", "fa_contract": "KT1x"},
            }
        )
        failures += check(
            f"unmapped {group} contract resolves via group",
            offer is not None and offer.marketplace == want,
            f"got {offer.marketplace if offer else None!r}, want {want!r}",
        )
        failures += check(
            f"unmapped {group} contract stays in expiry scope",
            offer is not None and offer.marketplace in scope,
            f"scope={scope}",
        )

    return failures


async def check_teia_cadence():
    """Drive the real Scanner.run_forever over a simulated hour.

    Time is faked so a 1-hour schedule verifies in milliseconds, and the real
    loop is exercised rather than a copy of its logic.
    """
    from bot.scanner import Scanner

    print("\n== Teia cadence ==")
    failures = 0

    for interval, teia_interval, want in [
        (60, 600, 6),    # teia at t=0, 600, 1200, ... 3000
        (60, 60, 60),    # equal intervals -> every cycle
        (300, 600, 6),   # teia slower than scan, still every 600s
    ]:
        now = {"t": 0.0}
        seen = []

        async def fake_scan_all(_on_scanned, include_teia=True, _seen=seen):
            if now["t"] >= 3600:
                raise asyncio.CancelledError
            _seen.append(include_teia)

        async def fake_sleep(seconds, _now=now):
            _now["t"] += seconds

        scanner = Scanner.__new__(Scanner)  # no db/clients needed here
        scanner.scan_all = fake_scan_all
        scanner._lock = asyncio.Lock()

        try:
            # One call to run_forever; it stops when fake clock passes an hour.
            await scanner.run_forever(
                interval,
                None,
                teia_interval,
                _clock=lambda: now["t"],
                _sleep=fake_sleep,
            )
        except asyncio.CancelledError:
            pass

        got = sum(seen)
        failures += check(
            f"scan={interval}s teia={teia_interval}s -> {want} teia pass(es)/hour",
            got == want,
            f"got {got} in {len(seen)} cycles",
        )

    return failures


async def check_min_threshold(db):
    """The /min threshold must gate alerts without hiding stored offers."""
    print("\n== Alert threshold ==")
    failures = 0
    uid = 999_000_001

    failures += check(
        "default threshold alerts on everything",
        await db.get_min_alert_mutez(uid) == 0,
    )

    await db.set_min_alert_mutez(uid, 5_000_000)
    failures += check(
        "threshold persists as mutez",
        await db.get_min_alert_mutez(uid) == 5_000_000,
        f"{await db.get_min_alert_mutez(uid)} mutez",
    )

    await db.set_min_alert_mutez(uid, -3)
    failures += check(
        "negative threshold clamps to 0",
        await db.get_min_alert_mutez(uid) == 0,
    )

    # The real alert path must drop sub-threshold offers, including the
    # "...and N more" count, while /offers keeps them.
    from bot.models import Offer
    from bot.scanner import WalletScan

    sent = []

    class FakeBot:
        async def send_message(self, **kw):
            sent.append(kw.get("text", ""))

        async def send_photo(self, **kw):
            sent.append(kw.get("caption", ""))

    class FakePrices:
        async def xtz_to_usd(self):
            return 0.30

    def make(price_mutez, i):
        return Offer(
            marketplace="objkt",
            offer_id=str(i),
            token_pk=str(i),
            token_id=str(i),
            contract="KT1x",
            token_name=f"Token {i}",
            media_uri=None,
            price_mutez=price_mutez,
            buyer="tz1buyer",
        )

    # 6 XTZ and 7 XTZ clear a 5 XTZ threshold; 0.1 XTZ and 4 XTZ do not.
    scan = WalletScan(
        telegram_id=uid,
        address="tz1wallet",
        nft_count=4,
        new_offers=[
            make(6_000_000, 1),
            make(100_000, 2),
            make(4_000_000, 3),
            make(7_000_000, 4),
        ],
        expired_count=0,
    )

    sent.clear()
    await notify_new_offers(scan, FakeBot(), FakePrices(), min_mutez=5_000_000)
    joined = "\n".join(sent)
    failures += check("sub-threshold offers are not sent", "Token 2" not in joined and "Token 3" not in joined)
    failures += check("at-or-above threshold offers are sent", "Token 1" in joined and "Token 4" in joined)

    sent.clear()
    await notify_new_offers(scan, FakeBot(), FakePrices(), min_mutez=0)
    joined = "\n".join(sent)
    failures += check(
        "threshold 0 sends everything",
        all(f"Token {i}" in joined for i in (1, 2, 3, 4)),
    )

    # An offer exactly at the threshold must alert: the user said "from 5 XTZ".
    sent.clear()
    await notify_new_offers(
        WalletScan(uid, "tz1wallet", 1, [make(5_000_000, 5)], 0),
        FakeBot(),
        FakePrices(),
        min_mutez=5_000_000,
    )
    failures += check(
        "offer exactly at threshold alerts",
        "Token 5" in "\n".join(sent),
    )

    # Everything below the threshold must still be stored and visible.
    addr = "tz1ThresholdCheck"
    await db.upsert_offers(uid, addr, [make(100_000, 11)], 0.3)
    stored = await db.list_offers(uid)
    failures += check(
        "sub-threshold offer is still stored for /offers",
        any(r["offer_id"] == "11" for r in stored),
        f"{len(stored)} stored",
    )
    await db._pool.execute("DELETE FROM offers WHERE telegram_id = $1", uid)
    await db._pool.execute("DELETE FROM settings WHERE telegram_id = $1", uid)

    return failures


async def main() -> int:
    failures = 0

    data_dir = pathlib.Path(tempfile.gettempdir()) / "pgdata_startup_test"
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(exist_ok=True)
    dsn = pgserver.get_server(data_dir).get_uri()

    print("\n== Database.connect + init_schema ==")
    db = await Database.connect(dsn)
    failures += check("connected and schema applied", True)

    rows = await db._pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    )
    tables = {r["table_name"] for r in rows}
    failures += check(
        "both tables exist", {"wallets", "offers"} <= tables, str(sorted(tables))
    )

    print("\n== unique constraint for dedupe ==")
    constraints = await db._pool.fetch(
        """
        SELECT constraint_name FROM information_schema.table_constraints
        WHERE table_name = 'offers' AND constraint_type = 'UNIQUE'
        """
    )
    names = {c["constraint_name"] for c in constraints}
    failures += check("offers has a UNIQUE constraint", len(names) >= 1, str(names))

    print("\n== Application wiring ==")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test-placeholder-not-used")

    async with httpx.AsyncClient(timeout=10.0) as http:
        objkt = ObjktClient(http, 100)
        teia = TeiaClient(http)
        prices = TzktPriceClient(http)
        scanner = Scanner(db, objkt, teia, prices)

        app = build_application(db, scanner, prices, objkt)
        failures += check("application built", app is not None)

        cmds = set()
        total = 0
        for group in app.handlers.values():
            for h in group:
                total += 1
                if hasattr(h, "commands"):
                    cmds.update(h.commands)

        expected = {"start", "track", "untrack", "wallet", "offers", "scan", "help"}
        missing = expected - cmds
        failures += check(
            "all commands registered",
            not missing,
            f"missing {missing}" if missing else f"{len(cmds)} commands",
        )
        failures += check("text message handler registered", total >= 8, f"{total} handlers")

        # Regression: the scanner task used to be created with a
        # create_task(update_interval=...) kwarg that PTB v21 does not accept,
        # so the bot died on every real boot. Building the app never caught it.
        # Parse the real call site in main.py so a bad kwarg fails here.
        import ast
        import inspect as _inspect

        from telegram.ext import Application as _Application

        accepted = set(_inspect.signature(_Application.create_task).parameters)
        main_py = pathlib.Path(__file__).resolve().parent.parent / "bot" / "main.py"
        tree = ast.parse(main_py.read_text(encoding="utf-8"))
        bad = [
            f"line {n.lineno}: {kw.arg}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "create_task"
            for kw in n.keywords
            if kw.arg and kw.arg not in accepted
        ]
        failures += check(
            "create_task kwargs in main.py are valid",
            not bad,
            "; ".join(bad) if bad else f"accepted={sorted(accepted)}",
        )

        failures += await check_teia_cadence()
        failures += check_expiry_scope()
        failures += await check_min_threshold(db)

        await app.shutdown()
        print("  (application shut down cleanly)")

    await db.close()
    failures += check("db closed cleanly", True)

    print("\n" + "=" * 46)
    if failures:
        print(f"RESULT: {failures} check(s) FAILED")
        return 1
    print("RESULT: all checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Telegram bot handlers and application wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import alerts
from bot.config import load_config
from bot.db import Database
from bot.scanner import NEW_OFFER_BATCH_LIMIT, Scanner, WalletScan
from bot.sources.objkt import ObjktClient
from bot.sources.teia import TeiaClient
from bot.sources.tzkt import TzktPriceClient
from bot.utils import format_xtz, is_valid_tezos_address

log = logging.getLogger(__name__)

escape = alerts.escape

WELCOME = (
    "👋 <b>Tezos Offers Bot</b>\n\n"
    "I watch the NFTs in your wallet and alert you the moment someone makes an "
    "offer on one.\n\n"
    "<b>Getting started</b>\n"
    "1. Send me your Tezos wallet address (tz1… or tz2…)\n"
    "2. I'll start scanning it\n\n"
    "<b>Commands</b>\n"
    "/track tz1… — add a wallet\n"
    "/untrack tz1… — stop tracking\n"
    "/wallet — your tracked wallets\n"
    "/offers — current active offers\n"
    "/min 5 — only message me for offers of 5 XTZ or more\n"
    "/scan — force a scan now\n"
    "/help — this message"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def track_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/track tz1...</code>", parse_mode=ParseMode.HTML
        )
        return

    address = context.args[0].strip()
    if not is_valid_tezos_address(address):
        await update.message.reply_text("That is not a valid Tezos address.")
        return

    await _track_wallet(update, context, address)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Treat any plain text that looks like an address as a wallet to track."""
    text = (update.message.text or "").strip()
    if not is_valid_tezos_address(text):
        await update.message.reply_text(
            "That doesn't look like a Tezos address. It should start with "
            "<code>tz1</code>, <code>tz2</code> or <code>tz3</code> and be 36 "
            "characters long.",
            parse_mode=ParseMode.HTML,
        )
        return

    await _track_wallet(update, context, text)


async def _track_wallet(
    update: Update, context: ContextTypes.DEFAULT_TYPE, address: str
) -> None:
    db: Database = context.application.bot_data["db"]
    objkt: ObjktClient = context.application.bot_data["objkt"]
    telegram_id = update.effective_user.id

    status = await update.message.reply_text("🔍 Checking your wallet…")

    try:
        holdings = await objkt.get_holdings(address)
    except Exception as exc:  # noqa: BLE001
        log.exception("holdings lookup failed")
        await status.edit_text(f"⚠️ Could not reach the data source: {exc}")
        return

    await db.add_wallet(telegram_id, address)
    await db.mark_scanned(telegram_id, address, len(holdings))

    if not holdings:
        await status.edit_text(
            f"👀 Tracking <code>{escape(address)}</code>\n\n"
            "I don't see any NFTs in this wallet yet. I'll still keep an eye on "
            "it — send me another address any time.",
            parse_mode=ParseMode.HTML,
        )
        return

    await status.edit_text(
        f"✅ Tracking <code>{escape(address)}</code>\n"
        f"Found <b>{len(holdings)}</b> NFT(s). Checking for offers now…",
        parse_mode=ParseMode.HTML,
    )

    # Scan immediately so the user gets instant value instead of waiting a cycle.
    scanner: Scanner = context.application.bot_data["scanner"]
    result = await scanner.scan_wallet(telegram_id, address)

    if result.error:
        await status.edit_text(
            f"⚠️ Tracking <code>{escape(address)}</code>, but the first scan "
            f"failed: {escape(result.error)}",
            parse_mode=ParseMode.HTML,
        )
        return

    await notify_pending_offers(
        result, context.bot, context.application.bot_data["prices"], db,
        context.application.bot_data["http"],
    )
    rows = [
        {
            "marketplace": o.marketplace,
            "price_mutez": o.price_mutez,
            "price_usd": None,
            "token_name": o.token_name,
            "token_id": o.token_id,
        }
        for o in result.new_offers
    ]
    await status.edit_text(
        f"✅ Tracking <b>{len(result.new_offers)}</b> new offer(s) across "
        f"{result.nft_count} NFT(s).\n\n"
        f"{alerts.build_summary(rows, None)}",
        parse_mode=ParseMode.HTML,
    )


async def untrack_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/untrack tz1...</code>", parse_mode=ParseMode.HTML
        )
        return

    db: Database = context.application.bot_data["db"]
    removed = await db.remove_wallet(update.effective_user.id, context.args[0].strip())
    await update.message.reply_text("Removed." if removed else "That wallet was not being tracked.")


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    rows = await db.list_wallets(update.effective_user.id)

    if not rows:
        await update.message.reply_text(
            "You're not tracking any wallets yet.\n\nSend me a Tezos address to get started."
        )
        return

    lines = ["👛 <b>Your wallets</b>", ""]
    for row in rows:
        label = f" ({row['label']})" if row["label"] else ""
        lines.append(f"• <code>{escape(row['address'])}</code>{label}")
        lines.append(f"  {row['nft_count']} NFT(s) tracked")
    lines.append("")
    lines.append("Send /untrack tz1… to stop tracking one.")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def offers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    prices: TzktPriceClient = context.application.bot_data["prices"]

    rows = await db.list_offers(update.effective_user.id, limit=50)
    if not rows:
        await update.message.reply_text(
            "💼 No active offers right now.\n\n"
            "When someone bids on one of your NFTs, you'll get a message here."
        )
        return

    usd_rate = await prices.xtz_to_usd()
    await update.message.reply_text(
        alerts.build_summary(rows, usd_rate), parse_mode=ParseMode.HTML
    )


async def min_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the minimum offer value that triggers an alert.

    Offers below the threshold are still recorded and still show up in /offers;
    they just do not send a message. That keeps the threshold a notification
    preference rather than a change to what the bot tracks.
    """
    db: Database = context.application.bot_data["db"]
    telegram_id = update.effective_user.id

    if not context.args:
        mutez = await db.get_min_alert_mutez(telegram_id)
        current = "every offer" if not mutez else f"{format_xtz(mutez)} XTZ"
        await update.message.reply_text(
            f"🔔 I'll message you for offers of <b>{escape(current)}</b> or more.\n\n"
            "Change it with <code>/min 5</code> (in XTZ), or "
            "<code>/min 0</code> to hear about everything.\n"
            "Offers below the threshold are still saved — see them with /offers.",
            parse_mode=ParseMode.HTML,
        )
        return

    raw = context.args[0].strip().lower()
    if raw in {"off", "none", "all"}:
        mutez = 0
    else:
        try:
            value = float(raw)
        except ValueError:
            await update.message.reply_text(
                "🤔 That doesn't look like a number.\n"
                "Try <code>/min 5</code> for 5 XTZ, or <code>/min 0</code> "
                "to be told about every offer.",
                parse_mode=ParseMode.HTML,
            )
            return
        if value < 0 or value != value or value == float("inf"):
            await update.message.reply_text(
                "🤔 Please use a positive number of XTZ, e.g. <code>/min 2.5</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        mutez = int(round(value * 1_000_000))

    await db.set_min_alert_mutez(telegram_id, mutez)
    if mutez:
        await update.message.reply_text(
            f"✅ I'll only message you for offers of <b>{format_xtz(mutez)} XTZ</b> "
            "or more. Everything below that is still saved and visible in /offers.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "✅ I'll message you about <b>every</b> new offer, however small.",
            parse_mode=ParseMode.HTML,
        )


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    scanner: Scanner = context.application.bot_data["scanner"]
    db: Database = context.application.bot_data["db"]
    telegram_id = update.effective_user.id

    status = await update.message.reply_text("🔄 Scanning your wallets…")
    rows = await db.list_wallets(telegram_id)
    min_mutez = await db.get_min_alert_mutez(telegram_id)

    total_new = 0
    total_below = 0
    for row in rows:
        result = await scanner.scan_wallet(telegram_id, row["address"])
        await notify_pending_offers(
            result, context.bot, context.application.bot_data["prices"], db,
            context.application.bot_data["http"],
        )
        for offer in result.new_offers:
            if offer.price_mutez >= min_mutez:
                total_new += 1
            else:
                total_below += 1

    if total_new:
        text = f"✅ Found <b>{total_new}</b> new offer(s)."
        if total_below:
            text += f"\n🤫 {total_below} below your /min threshold, not sent."
        await status.edit_text(text, parse_mode=ParseMode.HTML)
    elif total_below:
        await status.edit_text(
            f"🤫 Found <b>{total_below}</b> new offer(s), all below your /min "
            f"threshold of {format_xtz(min_mutez)} XTZ.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await status.edit_text("✅ Scan complete. No new offers.")


async def on_alert_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the View link on an offer alert."""
    query = update.callback_query
    await query.answer()

    url = query.data or ""
    if url.startswith("http"):
        await query.edit_message_text(
            f"Open this link to act on the offer:\n{url}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )


async def notify_new_offers(
    scan: WalletScan,
    bot,
    prices: TzktPriceClient,
    min_mutez: int = 0,
    http: httpx.AsyncClient | None = None,
) -> list[tuple[str, str]]:
    """Send alerts for a finished wallet scan.

    Offers under `min_mutez` are dropped here, at notify time, so they are
    still stored and still counted by /offers. Every alert path goes through
    this function, so one guard covers the poller and /scan.
    """
    if not scan.new_offers:
        return []

    new_offers = [o for o in scan.new_offers if o.price_mutez >= min_mutez]
    acknowledged = [o.key for o in scan.new_offers if o.price_mutez < min_mutez]
    if not new_offers:
        return acknowledged

    usd_rate = await prices.xtz_to_usd()

    for offer in new_offers[:NEW_OFFER_BATCH_LIMIT]:
        payload = alerts.build_alert(offer, usd_rate)
        # Telegram's own servers fail to fetch IPFS gateway URLs, so the
        # bytes are downloaded here and uploaded. A dead gateway then costs a
        # missing preview instead of a failed message.
        preview = await alerts.fetch_preview(http, offer.media_uri) if http else None
        if preview:
            try:
                await bot.send_photo(
                    chat_id=scan.telegram_id, photo=preview,
                    caption=payload["text"], parse_mode=payload["parse_mode"],
                )
                acknowledged.append(offer.key)
                log.info("sent offer %s to Telegram chat %s", offer.offer_id, scan.telegram_id)
                continue
            except Exception:  # A broken NFT preview must not hide the offer.
                log.warning("offer %s preview failed; sending text", offer.offer_id)
        try:
            await bot.send_message(
                chat_id=scan.telegram_id, text=payload["text"],
                parse_mode=payload["parse_mode"], disable_web_page_preview=True,
            )
            acknowledged.append(offer.key)
            log.info("sent offer %s to Telegram chat %s", offer.offer_id, scan.telegram_id)
        except Exception as exc:
            log.warning("offer %s delivery failed (%s); will retry", offer.offer_id, type(exc).__name__)

    remaining = len(new_offers) - NEW_OFFER_BATCH_LIMIT
    if remaining > 0:
        try:
            await bot.send_message(
                chat_id=scan.telegram_id,
                text=f"…and {remaining} more new offer(s). Use /offers to see them all.",
            )
            acknowledged.extend(o.key for o in new_offers[NEW_OFFER_BATCH_LIMIT:])
        except Exception as exc:
            log.warning("offer summary delivery failed (%s); will retry", type(exc).__name__)
    return acknowledged


async def notify_pending_offers(
    scan: WalletScan,
    bot,
    prices: TzktPriceClient,
    db: Database,
    http: httpx.AsyncClient | None = None,
) -> None:
    if scan.error:
        return
    pending = await db.pending_offers(scan.telegram_id, scan.address)
    if not pending:
        return
    delivery = WalletScan(scan.telegram_id, scan.address, scan.nft_count, pending, 0)
    keys = await notify_new_offers(
        delivery, bot, prices, await db.get_min_alert_mutez(scan.telegram_id), http
    )
    await db.acknowledge_alerts(scan.telegram_id, keys)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error", exc_info=context.error)


def build_application(
    db: Database, scanner: Scanner, prices: TzktPriceClient, objkt: ObjktClient
) -> Application:
    config = load_config()
    app = Application.builder().token(config.telegram_bot_token).build()

    app.bot_data["db"] = db
    app.bot_data["scanner"] = scanner
    app.bot_data["prices"] = prices
    app.bot_data["objkt"] = objkt

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("track", track_cmd))
    app.add_handler(CommandHandler("untrack", untrack_cmd))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("offers", offers_cmd))
    app.add_handler(CommandHandler("min", min_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(on_alert_click))
    app.add_error_handler(error_handler)

    return app


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs, and every Telegram API URL carries the bot
    # token. Keep its INFO chatter out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = load_config()

    db = await Database.connect(config.database_url)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": "tezos-offers-bot/1.0"},
    ) as http:
        objkt = ObjktClient(http, config.objkt_rate_limit_rpm)
        teia = TeiaClient(http)
        prices = TzktPriceClient(http)

        scanner = Scanner(db, objkt, teia, prices)
        app = build_application(db, scanner, prices, objkt)
        # Shared client, also used to download NFT previews for Telegram.
        app.bot_data["http"] = http
        bot = app.bot
        log.info("starting scanner every %ds", config.scan_interval)

        async def on_scanned(scan: WalletScan) -> None:
            if scan.error:
                log.warning("scan error for %s: %s", scan.address, scan.error)
                return
            await notify_pending_offers(scan, bot, prices, db, http)

        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            # Created after start() so PTB tracks and cancels it on shutdown.
            # The scanner sleeps before its first pass, so nothing is missed.
            app.create_task(
                scanner.run_forever(
                    config.scan_interval, on_scanned, config.teia_scan_interval
                ),
            )
            log.info("bot is running")
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.shutdown()
            await db.close()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(run())

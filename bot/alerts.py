"""Telegram message formatting for offers."""

from __future__ import annotations

import httpx

from bot.models import Offer
from bot.utils import format_usd, format_xtz, resolve_media_uri, shorten_address

# Deep links that let the user act on the offer from Telegram.
MARKET_URLS = {
    "objkt": "https://objkt.com/asset/{contract}/{token_id}",
    "fxhash": "https://fxhash.xyz/asset/{contract}/{token_id}",
    "hic et nunc": "https://hicte.nunc.art/asset/{contract}/{token_id}",
    "teia": "https://teia.art/objkt/{token_id}",
    "akaSwap": "https://akaswap.io/token/{contract}/{token_id}",
    "dogami": "https://dogami.io/asset/{contract}/{token_id}",
}


def offer_url(offer: Offer) -> str:
    template = MARKET_URLS.get(offer.marketplace)
    if not template:
        return ""
    return template.format(contract=offer.contract, token_id=offer.token_id)


def format_offer_caption(offer: Offer, usd_rate: float | None) -> str:
    """Human-readable one-liner block describing a single offer."""
    name = offer.token_name or f"Token #{offer.token_id}"
    price = f"{format_xtz(offer.price_mutez)} XTZ"

    if usd_rate:
        usd = offer.price_mutez / 1_000_000 * usd_rate
        price += f"  (~{format_usd(usd)})"

    lines = [
        f"<b>{escape(name)}</b>",
        f"💰 {price}",
        f"👤 {shorten_address(offer.buyer)}",
    ]
    return "\n".join(lines)


def build_alert(offer: Offer, usd_rate: float | None) -> dict:
    """Message payload for a new offer, with preview when the media resolves."""
    caption = (
        "🎯 <b>New offer received</b>\n\n"
        f"{format_offer_caption(offer, usd_rate)}\n"
        f"🏪 {escape(offer.marketplace)}"
    )

    url = offer_url(offer)
    if url:
        caption += f"\n🔗 <a href=\"{url}\">View on {escape(offer.marketplace)}</a>"

    payload: dict = {
        "text": caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    return payload


async def fetch_preview(http: httpx.AsyncClient, uri: str | None) -> bytes | None:
    """Download an NFT image so Telegram never has to fetch the gateway itself."""
    url = resolve_media_uri(uri)
    if not url:
        return None
    try:
        response = await http.get(url, timeout=20.0, follow_redirects=True)
        response.raise_for_status()
    except Exception:  # noqa: BLE001 - a missing preview must not hide the offer
        return None
    if not response.headers.get("content-type", "").startswith("image/"):
        return None
    return response.content


def build_summary(offers: list, usd_rate: float | None) -> str:
    """Compact list of several offers for one wallet."""
    if not offers:
        return "No active offers right now."

    lines = [f"💼 <b>{len(offers)} active offer(s)</b>", ""]
    for offer in offers:
        # Accepts asyncpg.Record or plain dict from a hand-built list.
        name = _field(offer, "token_name") or f"#{_field(offer, 'token_id')}"
        price = f"{format_xtz(_field(offer, 'price_mutez'))} XTZ"
        usd_value = _field(offer, "price_usd")
        if usd_value:
            price += f" (~{format_usd(usd_value)})"
        lines.append(
            f"• <b>{escape(name)}</b> — {price} "
            f"<i>{escape(_field(offer, 'marketplace'))}</i>"
        )

    if usd_rate:
        total = sum(_field(offer, "price_mutez") for offer in offers) / 1_000_000
        lines.append("")
        lines.append(f"Total: {total:.3f} XTZ (~{format_usd(total * usd_rate)})")

    return "\n".join(lines)


def _field(row, name: str):
    """Read a field from an asyncpg.Record, dict, or Offer object."""
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def escape(text: str) -> str:
    """Escape HTML special chars for Telegram's HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

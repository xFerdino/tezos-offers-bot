"""Minimal Tezos helpers: address validation, mutez formatting, IPFS URLs."""

from __future__ import annotations

import re

from bot.config import TEZOS_ADDRESS_RE

_ADDRESS_RE = re.compile(TEZOS_ADDRESS_RE)

MUTEZ_PER_XTZ = 1_000_000


def is_valid_tezos_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value.strip()))


def normalize_address(value: str) -> str:
    return value.strip()


def xtz_from_mutez(mutez: int | None) -> float | None:
    if mutez is None:
        return None
    return round(mutez / MUTEZ_PER_XTZ, 6)


def format_xtz(mutez: int | None, max_decimals: int = 3) -> str:
    """Format mutez as a human XTZ string, trimming trailing zeros."""
    if mutez is None:
        return "?"
    xtz = mutez / MUTEZ_PER_XTZ
    if xtz == 0:
        return "0"
    text = f"{xtz:.{max_decimals}f}".rstrip("0").rstrip(".")
    return text or "0"


def format_usd(value: float | None) -> str:
    if value is None:
        return ""
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def shorten_address(address: str | None, head: int = 6, tail: int = 4) -> str:
    if not address:
        return "unknown"
    if len(address) <= head + tail + 1:
        return address
    return f"{address[:head]}...{address[-tail:]}"


def resolve_media_uri(uri: str | None) -> str | None:
    """Turn an ipfs:// or ar:// URI into something Telegram can fetch.

    Telegram only accepts public http(s) URLs, so gateway prefixes are applied.
    Returns None when the URI is empty or an unsupported scheme.
    """
    if not uri:
        return None

    uri = uri.strip()
    if uri.startswith(("http://", "https://")):
        return uri

    for scheme in ("ipfs://", "ar://"):
        if uri.startswith(scheme):
            cid = uri[len(scheme):]
            if scheme == "ipfs://":
                # Cloudflare's gateway is dead: the hostname no longer resolves,
                # which is why previews vanished while the code stayed unchanged.
                return f"https://gateway.pinata.cloud/ipfs/{cid}"
            return f"https://arweave.net/{cid}"

    # Bare CID, or a data: URI we cannot render.
    if re.fullmatch(r"[A-Za-z0-9]{46,}", uri):
        return f"https://gateway.pinata.cloud/ipfs/{uri}"
    return None

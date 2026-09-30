"""Poll loop: fetch holdings, resolve offers, diff, and report new ones."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from bot.db import Database
from bot.models import Holding, Offer
from bot.sources.objkt import ObjktClient
from bot.sources.objkt import GROUP_LABELS
from bot.sources.teia import TeiaClient
from bot.sources.tzkt import TzktPriceClient

log = logging.getLogger(__name__)

# Notify after this many offers for one wallet instead of spamming individually.
NEW_OFFER_BATCH_LIMIT = 10


def obkt_expiry_scope() -> tuple[str, ...]:
    """The objkt-side marketplace labels that expiry should cover.

    Fixed set, not derived from live results: a marketplace with no offers in
    this scan still has stored offers that may have gone stale. objkt's offer
    query applies no group filter, so every group in GROUP_LABELS can appear
    (akaSwap included) and all of them must be in scope.
    """
    return tuple(dict.fromkeys(GROUP_LABELS.values()))

@dataclass
class WalletScan:
    telegram_id: int
    address: str
    nft_count: int
    new_offers: list[Offer]
    expired_count: int
    error: str | None = None


class Scanner:
    def __init__(
        self,
        db: Database,
        objkt: ObjktClient,
        teia: TeiaClient,
        prices: TzktPriceClient,
    ) -> None:
        self._db = db
        self._objkt = objkt
        self._teia = teia
        self._prices = prices
        self._lock = asyncio.Lock()

    async def scan_wallet(
        self, telegram_id: int, address: str, include_teia: bool = True
    ) -> WalletScan:
        """Scan one wallet for offers, persisting state and returning what is new."""
        # Only one scan per bot at a time; overlapping scans would race on the
        # dedupe table and burn the objkt rate limit.
        async with self._lock:
            try:
                return await self._scan_wallet_locked(
                    telegram_id, address, include_teia
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the caller
                log.exception("scan failed for %s", address)
                return WalletScan(telegram_id, address, 0, [], 0, error=str(exc))

    async def _scan_wallet_locked(
        self, telegram_id: int, address: str, include_teia: bool = True
    ) -> WalletScan:
        holdings = await self._objkt.get_holdings(address)
        log.info("wallet %s holds %d NFTs", address[:10], len(holdings))

        if not holdings:
            await self._db.mark_scanned(telegram_id, address, 0)
            return WalletScan(telegram_id, address, 0, [], 0)

        # objkt-indexed marketplaces (objkt, fxhash, HEN, akaSwap) in one call.
        objkt_offers = await self._objkt.get_offers_for_wallet(address, holdings)

        # Teia is a separate indexer, so it needs its own pass. If Teia is
        # unavailable we must not expire its offers, or the next successful
        # scan would re-alert every one of them as new.
        teia_offers: list[Offer] = []
        # A skipped Teia pass is not a failed one: its offers must not be
        # expired, or the next real pass would re-alert every one of them.
        teia_available = False
        if include_teia:
            try:
                teia_offers = self._attach_teia_context(
                    await self._teia.get_offers_for_holdings(holdings), holdings
                )
                teia_available = True
            except Exception as exc:  # noqa: BLE001 - teia is a nice-to-have source
                log.warning("teia scan failed for %s: %s", address, exc)

        offers = objkt_offers + teia_offers

        usd_rate = await self._prices.xtz_to_usd()
        new_offers = await self._db.upsert_offers(telegram_id, address, offers, usd_rate)

        # Expire per source, and skip Teia entirely when it did not answer.
        # Scope must be the marketplaces we poll, NOT the ones that happen to
        # have a live offer right now: deriving it from objkt_offers meant a
        # marketplace with zero visible offers dropped out of scope, leaving
        # its stored offers stuck active forever.
        expired = await self._db.mark_expired(
            telegram_id,
            address,
            {o.key for o in objkt_offers},
            marketplaces=list(obkt_expiry_scope()),
        )
        if teia_available:
            expired += await self._db.mark_expired(
                telegram_id, address, {o.key for o in teia_offers}, marketplaces=["teia"]
            )

        await self._db.mark_scanned(telegram_id, address, len(holdings))

        log.info(
            "wallet %s: %d offers found, %d new, %d expired",
            address[:10],
            len(offers),
            len(new_offers),
            expired,
        )
        return WalletScan(telegram_id, address, len(holdings), new_offers, expired)

    @staticmethod
    def _attach_teia_context(
        teia_offers: list[Offer], holdings: list[Holding]
    ) -> list[Offer]:
        """Teia rows have no token name, so match them to known holdings.

        Only an offer whose (contract, token_id) matches something we actually
        hold is kept, which also filters out Teia collection-level offers.
        """
        by_key = {(h.contract, h.token_id): h for h in holdings}

        matched: list[Offer] = []
        for offer in teia_offers:
            holding = by_key.get((offer.contract, offer.token_id))
            if not holding:
                continue
            matched.append(
                Offer(
                    marketplace=offer.marketplace,
                    offer_id=offer.offer_id,
                    token_pk=holding.token_pk,
                    token_id=holding.token_id,
                    contract=offer.contract,
                    token_name=holding.name,
                    media_uri=holding.media_uri,
                    price_mutez=offer.price_mutez,
                    buyer=offer.buyer,
                )
            )
        return matched

    async def scan_all(
        self,
        on_wallet_scanned: Callable[[WalletScan], Awaitable[None]],
        include_teia: bool = True,
    ) -> None:
        """Scan every tracked wallet, notifying per wallet as results land."""
        wallets = await self._db.all_wallets()
        if not wallets:
            log.info("no wallets tracked yet")
            return

        log.info(
            "scanning %d wallet(s) (teia=%s)", len(wallets), "on" if include_teia else "skipped"
        )
        for row in wallets:
            result = await self.scan_wallet(
                row["telegram_id"], row["address"], include_teia
            )
            await on_wallet_scanned(result)

    async def run_forever(
        self,
        interval: int,
        on_wallet_scanned: Callable[[WalletScan], Awaitable[None]],
        teia_interval: int | None = None,
        _clock: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Poll on a fixed interval, recovering from scan errors.

        Teia costs one query per held token, so it runs on its own slower
        cadence. ponytail: one interval counter per loop; a scheduler would be
        overkill for two cadences.
        """
        if teia_interval is None or teia_interval <= 0:
            teia_interval = interval

        # teia_interval seconds must pass between Teia passes. Seeded at -inf
        # so the very first cycle always includes Teia.
        last_teia = float("-inf")
        while True:
            include_teia = (_clock() - last_teia) >= teia_interval
            try:
                # Stamp the START of the pass, not the end: a Teia pass over a
                # large wallet takes minutes, and stamping on completion would
                # stretch the effective interval to teia_interval + duration.
                started = _clock()
                await self.scan_all(on_wallet_scanned, include_teia=include_teia)
                if include_teia:
                    last_teia = started
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("scan cycle failed; continuing")

            await _sleep(interval)
        while True:
            try:
                await self.scan_all(on_wallet_scanned)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("scan cycle failed; continuing")

            await asyncio.sleep(interval)

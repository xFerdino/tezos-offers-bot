"""PostgreSQL access layer using asyncpg."""

from __future__ import annotations

import logging
from typing import Any, Iterable

import asyncpg

from bot.models import Offer

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id              SERIAL PRIMARY KEY,
    telegram_id     BIGINT NOT NULL,
    address         TEXT   NOT NULL,
    label           TEXT,
    nft_count       INTEGER NOT NULL DEFAULT 0,
    last_scanned_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (telegram_id, address)
);

CREATE INDEX IF NOT EXISTS idx_wallets_telegram ON wallets (telegram_id);

CREATE TABLE IF NOT EXISTS offers (
    id             SERIAL PRIMARY KEY,
    telegram_id    BIGINT NOT NULL,
    address        TEXT   NOT NULL,
    marketplace    TEXT   NOT NULL,
    offer_id       TEXT   NOT NULL,
    token_pk       TEXT,
    token_id       TEXT   NOT NULL,
    contract       TEXT   NOT NULL,
    token_name     TEXT,
    media_uri      TEXT,
    price_mutez    BIGINT NOT NULL,
    price_usd      DOUBLE PRECISION,
    buyer          TEXT,
    status         TEXT   NOT NULL DEFAULT 'active',
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Dedupe: one row per (telegram user, marketplace, offer id).
    UNIQUE (telegram_id, marketplace, offer_id)
);

CREATE INDEX IF NOT EXISTS idx_offers_status ON offers (telegram_id, status);
CREATE INDEX IF NOT EXISTS idx_offers_address ON offers (telegram_id, address, status);

-- Existing history stays acknowledged; new/revived offers opt into delivery.
ALTER TABLE offers ADD COLUMN IF NOT EXISTS alert_pending BOOLEAN NOT NULL DEFAULT FALSE;

-- Per-user alert threshold. All offers are still stored and shown by /offers;
-- this only decides which NEW ones are announced.
CREATE TABLE IF NOT EXISTS settings (
    telegram_id     BIGINT PRIMARY KEY,
    min_alert_mutez BIGINT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class Database:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Database":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        db = cls(pool)
        await db.init_schema()
        return db

    async def init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA)
        log.info("database schema ready")

    async def close(self) -> None:
        await self._pool.close()

    # --- wallets ---------------------------------------------------------

    async def add_wallet(
        self, telegram_id: int, address: str, label: str | None = None
    ) -> None:
        await self._pool.execute(
            """
            INSERT INTO wallets (telegram_id, address, label)
            VALUES ($1, $2, $3)
            ON CONFLICT (telegram_id, address) DO UPDATE SET label = EXCLUDED.label
            """,
            telegram_id,
            address,
            label,
        )

    async def remove_wallet(self, telegram_id: int, address: str) -> bool:
        result = await self._pool.execute(
            "DELETE FROM wallets WHERE telegram_id = $1 AND address = $2",
            telegram_id,
            address,
        )
        return result.endswith("1")

    async def list_wallets(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self._pool.fetch(
            """
            SELECT address, label, nft_count, last_scanned_at
            FROM wallets
            WHERE telegram_id = $1
            ORDER BY created_at
            """,
            telegram_id,
        )

    async def all_wallets(self) -> list[asyncpg.Record]:
        return await self._pool.fetch(
            "SELECT telegram_id, address FROM wallets ORDER BY telegram_id"
        )

    async def mark_scanned(self, telegram_id: int, address: str, nft_count: int) -> None:
        await self._pool.execute(
            """
            UPDATE wallets
            SET last_scanned_at = NOW(), nft_count = $3
            WHERE telegram_id = $1 AND address = $2
            """,
            telegram_id,
            address,
            nft_count,
        )

    # --- offers ----------------------------------------------------------

    async def known_offer_keys(
        self, telegram_id: int, address: str
    ) -> set[tuple[str, str]]:
        """(marketplace, offer_id) pairs already recorded for a wallet."""
        rows = await self._pool.fetch(
            """
            SELECT marketplace, offer_id
            FROM offers
            WHERE telegram_id = $1 AND address = $2 AND status = 'active'
            """,
            telegram_id,
            address,
        )
        return {(r["marketplace"], r["offer_id"]) for r in rows}

    async def upsert_offers(
        self,
        telegram_id: int,
        address: str,
        offers: Iterable[Offer],
        usd_rate: float | None,
    ) -> list[Offer]:
        """Insert new offers and refresh seen ones. Returns the newly added.

        Dedupe is the `WHERE offers.status = 'expired'` guard: a still-active
        offer is neither updated nor returned, so it is never re-alerted. An
        offer that had expired and is live again is reactivated and returned,
        so the user hears about it again.
        """
        offer_list = list(offers)
        if not offer_list:
            return []

        rows: list[tuple] = []
        for offer in offer_list:
            price_usd = None
            if usd_rate:
                price_usd = round(offer.price_mutez / 1_000_000 * usd_rate, 2)
            rows.append(
                (
                    telegram_id,
                    address,
                    offer.marketplace,
                    offer.offer_id,
                    offer.token_pk,
                    offer.token_id,
                    offer.contract,
                    offer.token_name,
                    offer.media_uri,
                    offer.price_mutez,
                    price_usd,
                    offer.buyer,
                )
            )

        inserted = await self._pool.fetch(
            """
            INSERT INTO offers (
                telegram_id, address, marketplace, offer_id, token_pk, token_id,
                contract, token_name, media_uri, price_mutez, price_usd, buyer, alert_pending
            )
            SELECT *, TRUE FROM UNNEST(
                $1::bigint[], $2::text[], $3::text[], $4::text[], $5::text[],
                $6::text[], $7::text[], $8::text[], $9::text[], $10::bigint[],
                $11::double precision[], $12::text[]
            )
            ON CONFLICT (telegram_id, marketplace, offer_id) DO UPDATE
                SET status = 'active',
                    alert_pending = TRUE,
                    last_seen_at = NOW(),
                    price_mutez = EXCLUDED.price_mutez,
                    price_usd = EXCLUDED.price_usd,
                    buyer = EXCLUDED.buyer,
                    token_name = COALESCE(EXCLUDED.token_name, offers.token_name)
            -- Only touch rows that are currently expired. An offer that is
            -- still active is left alone and, crucially, is not returned, so
            -- it is never re-alerted. Every returned row is therefore either
            -- brand new or previously expired and now live again.
            WHERE offers.status = 'expired'
            RETURNING marketplace, offer_id
            """,
            *[list(col) for col in zip(*rows)],
        )

        inserted_keys = {(r["marketplace"], r["offer_id"]) for r in inserted}
        return [o for o in offer_list if o.key in inserted_keys]

    async def pending_offers(self, telegram_id: int, address: str) -> list[Offer]:
        rows = await self._pool.fetch(
            """SELECT marketplace, offer_id, token_pk, token_id, contract,
                      token_name, media_uri, price_mutez, buyer
               FROM offers WHERE telegram_id = $1 AND address = $2
                 AND status = 'active' AND alert_pending
               ORDER BY first_seen_at DESC, id""",
            telegram_id, address,
        )
        return [Offer(**dict(row)) for row in rows]

    async def acknowledge_alerts(
        self, telegram_id: int, keys: list[tuple[str, str]]
    ) -> None:
        await self._pool.execute(
            """UPDATE offers SET alert_pending = FALSE
               WHERE telegram_id = $1 AND (marketplace, offer_id) IN (
                   SELECT * FROM UNNEST($2::text[], $3::text[])
               )""",
            telegram_id,
            [market for market, _ in keys],
            [offer_id for _, offer_id in keys],
        )

    async def get_min_alert_mutez(self, telegram_id: int) -> int:
        """Alert threshold in mutez. 0 means alert on every offer."""
        return await self._pool.fetchval(
            "SELECT min_alert_mutez FROM settings WHERE telegram_id = $1",
            telegram_id,
        ) or 0

    async def set_min_alert_mutez(self, telegram_id: int, mutez: int) -> None:
        await self._pool.execute(
            """
            INSERT INTO settings (telegram_id, min_alert_mutez, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (telegram_id) DO UPDATE
                SET min_alert_mutez = EXCLUDED.min_alert_mutez, updated_at = NOW()
            """,
            telegram_id,
            max(0, mutez),
        )

    async def mark_expired(
        self,
        telegram_id: int,
        address: str,
        active_keys: set[tuple[str, str]],
        marketplaces: list[str] | None = None,
    ) -> int:
        """Expire offers that are no longer active, scoped to `marketplaces`.

        Scoping matters: when one data source is down we must not expire the
        offers it reported, or the next healthy scan would re-alert them all.
        """
        if marketplaces is None:
            marketplaces = list({k[0] for k in active_keys})

        keys = sorted(active_keys)

        result = await self._pool.execute(
            """
            UPDATE offers
            SET status = 'expired'
            WHERE telegram_id = $1
              AND address = $2
              AND status = 'active'
              AND marketplace = ANY($3::text[])
              AND NOT (
                    (marketplace, offer_id) IN (
                        SELECT * FROM UNNEST($4::text[], $5::text[])
                    )
                  )
            """,
            telegram_id,
            address,
            marketplaces,
            [market for market, _ in keys],
            [offer_id for _, offer_id in keys],
        )
        try:
            return int(result.split()[-1])
        except ValueError:
            return 0

    async def list_offers(
        self, telegram_id: int, limit: int = 50
    ) -> list[asyncpg.Record]:
        return await self._pool.fetch(
            """
            SELECT marketplace, offer_id, token_id, contract, token_name,
                   media_uri, price_mutez, price_usd, buyer, address
            FROM offers
            WHERE telegram_id = $1 AND status = 'active'
            ORDER BY price_mutez DESC
            LIMIT $2
            """,
            telegram_id,
            limit,
        )

    async def offer_stats(self, telegram_id: int) -> dict[str, Any]:
        row = await self._pool.fetchrow(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(price_mutez), 0) AS total_mutez,
                   COUNT(DISTINCT address) AS wallets
            FROM offers
            WHERE telegram_id = $1 AND status = 'active'
            """,
            telegram_id,
        )
        return {
            "total": row["total"],
            "total_mutez": row["total_mutez"],
            "wallets": row["wallets"],
        }

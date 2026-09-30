"""objkt data sources: NFT holdings and active offers.

objkt's public v3 GraphQL indexer also covers offers made on fxhash, hic et
nunc and akaSwap, so one query covers every marketplace we poll. Teia is NOT
in this indexer and is handled by teia.py.

Schema notes (verified against the live API):
  - comparison operators are underscore-prefixed: _eq, _in
  - token_pk is a bigint, so it must be passed as a string
  - token.holders filters token offers by wallet directly
  - project offers use collection_offer="agpk:<gallery pk>", with token=null
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from bot.config import OBJKT_GRAPHQL, OBJKT_MARKETPLACE_GROUPS
from bot.models import Holding, Offer

log = logging.getLogger(__name__)

# objkt paginates with limit/offset; 100 is well inside their 500 cap.
_HOLDINGS_PAGE = 100
_OFFERS_PAGE = 100

# objkt's own marketplace group -> the label we show and expire under.
GROUP_LABELS = {
    "objktcom": "objkt",
    "fxhash": "fxhash",
    "hen": "hic et nunc",
    "hic et nunc": "hic et nunc",
    "akaswap": "akaSwap",
    "dogami": "dogami",
}


class ObjktClient:
    def __init__(self, client: httpx.AsyncClient, rate_limit_rpm: int = 100) -> None:
        self._http = client
        # Simple token bucket: at most rate_limit_rpm requests per minute.
        self._interval = 60.0 / max(rate_limit_rpm, 1)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _throttle(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._last_call + self._interval - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = loop.time()

    async def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """POST a GraphQL query and return the data payload."""
        await self._throttle()
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._http.post(OBJKT_GRAPHQL, json=payload)
                response.raise_for_status()
                body = response.json()
                if "errors" in body and body["errors"]:
                    raise RuntimeError(f"objkt GraphQL error: {body['errors']}")
                return body.get("data") or {}
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))

        raise RuntimeError(f"objkt query failed after 3 attempts: {last_error}")

    async def get_holdings(self, address: str) -> list[Holding]:
        """All NFTs currently held by `address`, paginated."""
        query = """
        query Holdings($addr: String!, $limit: Int!, $offset: Int!) {
          token_holder(
            where: { holder_address: { _eq: $addr }, quantity: { _gt: 0 } }
            order_by: { token_pk: asc }
            limit: $limit
            offset: $offset
          ) {
            quantity
            token_pk
            token {
              name
              token_id
              fa_contract
              display_uri
              thumbnail_uri
              galleries { gallery { pk name } }
            }
          }
        }
        """

        holdings: list[Holding] = []
        offset = 0
        seen: set[str] = set()

        while True:
            data = await self._query(
                query,
                {"addr": address, "limit": _HOLDINGS_PAGE, "offset": offset},
            )
            rows = data.get("token_holder") or []
            if not rows:
                break

            for row in rows:
                token = row.get("token") or {}
                token_pk = row.get("token_pk")
                if token_pk is None:
                    # Deleted or migrated token - nothing to match offers against.
                    continue
                token_pk = str(token_pk)
                if token_pk in seen:
                    continue
                seen.add(token_pk)

                holdings.append(
                    Holding(
                        token_pk=token_pk,
                        token_id=str(token.get("token_id") or "0"),
                        contract=token.get("fa_contract") or "",
                        name=token.get("name"),
                        media_uri=token.get("display_uri") or token.get("thumbnail_uri"),
                        projects={
                            f"agpk:{g['gallery']['pk']}": g["gallery"].get("name") or "Project"
                            for g in token.get("galleries") or []
                            if g.get("gallery") and g["gallery"].get("pk") is not None
                        },
                    )
                )

            offset += _HOLDINGS_PAGE
            if len(rows) < _HOLDINGS_PAGE:
                break

        return holdings

    async def get_offers_for_wallet(
        self, address: str, holdings: list[Holding]
    ) -> list[Offer]:
        """Token offers filtered by owner, plus projects represented in the wallet."""
        by_project = {}
        for holding in holdings:
            for project in holding.projects:
                by_project.setdefault((holding.contract, project), holding)
        projects = list(dict.fromkeys(project for _, project in by_project))
        where = {
            "_or": [
                {"token": {"holders": {
                    "holder_address": {"_eq": address}, "quantity": {"_gt": 0}
                }}},
                {"collection_offer": {"_in": projects}},
            ],
            "_and": [{"_or": [
                {"target_address": {"_is_null": True}},
                {"target_address": {"_eq": address}},
            ]}],
        }
        query = """
        query WalletOffers($where: offer_active_bool_exp!, $limit: Int!, $offset: Int!) {
          offer_active(
            where: $where
            order_by: { id: desc }
            limit: $limit
            offset: $offset
          ) {
            id
            token_pk
            collection_offer
            fa_contract
            price_xtz
            buyer_address
            marketplace_contract
            level
            ophash
            marketplace { name group }
            token {
              name
              token_id
              fa_contract
              display_uri
              thumbnail_uri
            }
          }
        }
        """

        offers: dict[tuple[str, str], Offer] = {}
        offset = 0
        while True:
            data = await self._query(
                query, {"where": where, "limit": _OFFERS_PAGE, "offset": offset}
            )
            rows = data.get("offer_active") or []
            for row in rows:
                project = row.get("collection_offer")
                if project:
                    holding = by_project.get((row.get("fa_contract"), project))
                    if holding is None:
                        continue
                    # One alert per bid, even when several owned editions qualify.
                    row = {**row, "token_pk": holding.token_pk, "token": {
                        "fa_contract": holding.contract,
                        "token_id": holding.token_id,
                        "name": f"Project offer: {holding.projects[project]}",
                        "display_uri": holding.media_uri,
                    }}
                offer = self._parse_offer(row)
                if offer:
                    offers[offer.key] = offer
            if len(rows) < _OFFERS_PAGE:
                break
            offset += _OFFERS_PAGE

        return list(offers.values())

    @staticmethod
    def _parse_offer(row: dict) -> Offer | None:
        token = row.get("token") or {}
        offer_id = row.get("id")
        contract = token.get("fa_contract")

        if offer_id is None or not contract:
            return None

        from bot.config import MARKETPLACE_LABELS

        # Prefer objkt's own marketplace group: the hardcoded contract map goes
        # stale, and an unmapped contract used to become "unknown", which then
        # silently fell out of the per-marketplace expiry scope.
        group = ((row.get("marketplace") or {}).get("group") or "").strip().lower()
        marketplace = (
            MARKETPLACE_LABELS.get(row.get("marketplace_contract") or "")
            or (GROUP_LABELS.get(group) or group or "unknown")
        )

        return Offer(
            marketplace=marketplace,
            offer_id=str(offer_id),
            token_pk=str(row["token_pk"]) if row.get("token_pk") is not None else None,
            token_id=str(token.get("token_id") or "0"),
            contract=contract,
            token_name=token.get("name"),
            media_uri=token.get("display_uri") or token.get("thumbnail_uri"),
            price_mutez=int(row.get("price_xtz") or 0),
            buyer=row.get("buyer_address"),
            level=row.get("level"),
            op_hash=row.get("ophash"),
        )


def supported_marketplaces() -> tuple[str, ...]:
    return OBJKT_MARKETPLACE_GROUPS

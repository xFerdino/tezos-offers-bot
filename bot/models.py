"""Normalized data shapes shared across sources."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Holding:
    """One NFT held by a tracked wallet."""

    token_pk: str
    token_id: str
    contract: str
    name: str | None
    media_uri: str | None
    projects: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Offer:
    """An active offer, normalized across marketplaces.

    Dedupe key is (marketplace, offer_id).
    """

    marketplace: str
    offer_id: str
    token_pk: str | None
    token_id: str
    contract: str
    token_name: str | None
    media_uri: str | None
    price_mutez: int
    buyer: str | None
    level: int | None = None
    op_hash: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.marketplace, self.offer_id)

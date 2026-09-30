"""Regression: wallet/project offers must reach Telegram with valid arguments."""

import asyncio
import inspect
import json
import sys
from pathlib import Path

import httpx
from telegram import Bot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.main import notify_new_offers
from bot.alerts import offer_url
from bot.models import Offer
from bot.scanner import WalletScan
from bot.sources.objkt import ObjktClient


async def check_delivery():
    delivered = []

    class Telegram:
        photo_available = False

        async def send_message(self, **kwargs):
            inspect.signature(Bot.send_message).bind(self, **kwargs)
            delivered.append(kwargs)

        async def send_photo(self, **kwargs):
            inspect.signature(Bot.send_photo).bind(self, **kwargs)
            if self.photo_available:
                delivered.append(kwargs)
                return
            # A failed preview must fall back to the offer's text and link.
            raise RuntimeError("image unavailable")

    class Prices:
        async def xtz_to_usd(self):
            return None

    offer = Offer("objkt", "1669129", "18656666", "1530708", "KT1fx",
                  "Project offer: les villes", "https://example.com/nft.png",
                  12_000_000, "tz1buyer")
    teia_offer = Offer("teia", "1", None, "21411",
                       "KT1RJ6PbjHpwc3M5rw5s2Nbmefwbuwbdxton", "test", None,
                       1_000_000, "tz1buyer")
    assert offer_url(teia_offer) == "https://teia.art/objkt/21411"
    scan = WalletScan(42, "tz1wallet", 1, [offer], 0)
    telegram = Telegram()
    acknowledged = await notify_new_offers(scan, telegram, Prices())
    assert acknowledged == [("objkt", "1669129")]
    assert len(delivered) == 1, "offer must be delivered even if its image fails"
    assert delivered[0]["chat_id"] == 42
    assert "12 XTZ" in delivered[0]["text"]
    assert "https://objkt.com/asset/KT1fx/1530708" in delivered[0]["text"]
    delivered.clear()
    telegram.photo_available = True
    acknowledged = await notify_new_offers(scan, telegram, Prices())
    assert acknowledged == [("objkt", "1669129")]
    assert delivered[0]["chat_id"] == 42 and "12 XTZ" in delivered[0]["caption"]


async def check_wallet_projects():
    requests = []
    token = {"name": "les villes #15", "fa_contract": "KT1fx",
             "token_id": "1530708", "display_uri": None,
             "galleries": [{"gallery": {"pk": 17884040, "name": "les villes"}}]}

    def respond(request):
        payload = json.loads(request.content)
        variables = payload["variables"]
        requests.append(payload)
        if "token_holder(" in payload["query"]:
            return httpx.Response(200, json={"data": {"token_holder": [
                {"quantity": "1", "token_pk": "18656666", "token": token},
                {"quantity": "1", "token_pk": "18656667", "token": token},
            ]}})
        where = variables["where"]
        assert where["_or"][0]["token"]["holders"] == {
            "holder_address": {"_eq": "tz1wallet"}, "quantity": {"_gt": 0}}
        assert where["_or"][1]["collection_offer"]["_in"] == ["agpk:17884040"]
        rows = [{"id": 1669129, "token_pk": None, "token": None,
                 "collection_offer": "agpk:17884040", "fa_contract": "KT1fx",
                 "price_xtz": 12_000_000, "marketplace": {"group": "objktcom"}}]
        if variables["offset"]:
            rows = [{"id": 1669124, "token_pk": "18656666", "token": token,
                     "price_xtz": 500_000, "marketplace": {"group": "objktcom"}},
                    {"id": 999, "collection_offer": "agpk:17884040", "fa_contract": "KT1other"},
                    {"id": 998, "collection_offer": "agpk:999", "fa_contract": "KT1fx"}]
        else:
            rows *= 100  # A full page must not truncate the next page.
        return httpx.Response(200, json={"data": {"offer_active": rows}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ObjktClient(http, rate_limit_rpm=100000)
        holdings = await client.get_holdings("tz1wallet")
        assert hasattr(client, "get_offers_for_wallet"), "missing wallet/project offer lookup"
        offers = await client.get_offers_for_wallet("tz1wallet", holdings)
        assert [o.offer_id for o in offers] == ["1669129", "1669124"]
        assert offers[0].token_name == "Project offer: les villes"
        assert offers[0].token_id == "1530708"
        assert offers[0].price_mutez == 12_000_000
        assert len(requests) == 3, "one holdings page and two wallet offer pages"


async def main():
    failures = []
    for check in (check_delivery, check_wallet_projects):
        try:
            await check()
            print(f"PASS {check.__name__}")
        except Exception as exc:
            failures.append(check.__name__)
            print(f"FAIL {check.__name__}: {exc}")
    assert not failures, failures


if __name__ == "__main__":
    asyncio.run(main())

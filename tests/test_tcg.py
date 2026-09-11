"""TCGdex client, live."""

import asyncio
import time

from discord_bot_v3.services.tcgdex import TcgdexClient, asset_url


async def main():
    c = TcgdexClient()
    try:
        t0 = time.monotonic()
        n = await c.refresh()
        print(f"refresh: {n} sets, {c.card_count} cards in {time.monotonic() - t0:.1f}s")
        assert n >= 15 and c.card_count > 2000

        print("\nsets (oldest first):")
        for s in c.sets[:5]:
            print(f"   {s.id:5} {s.name[:28]:30} {s.release_date} {s.official}/{s.total} cards")
        print("   ...")
        s = c.get_set("A1")
        print("\nA1:", s.name, "| logo:", s.logo_url)
        print("   symbol:", s.symbol_url, "| cards cached:", len(s.cards))
        assert s.logo_url.endswith(".png") and len(s.cards) > 200

        print("\ncase-insensitive lookups:")
        print("   get_set('a1')      ->", c.get_set("a1").id)
        print("   find_stub('a1-001')->", c.find_stub("a1-001"))
        assert c.get_set("a1").id == "A1"
        assert c.find_stub("a1-001").name == "Bulbasaur"
        assert c.find_stub("nope-999") is None

        print("\nfull card A1-001:")
        card = await c.get_card("A1-001")
        print(f"   {card.name} | {card.category} | {card.rarity} | HP {card.hp} {card.types}")
        print("   stage:", card.stage, "| retreat:", card.retreat, "| dex:", card.dex_ids)
        print("   attacks:", [(a.name, a.damage, a.cost) for a in card.attacks])
        print("   weakness:", card.weaknesses, "| illustrator:", card.illustrator)
        print("   image:", card.image_url)
        assert card.image_url.endswith("/high.png") and card.attacks

        print("\ntrainer card (no hp/types):")
        t = await c.get_card("P-A-001")
        print(f"   {t.name} | {t.category} | trainer_type={t.trainer_type!r} | hp={t.hp}")
        print("   effect:", (t.effect or "")[:70])

        print("\nmissing card:", await c.get_card("ZZZ-999"))
        assert await c.get_card("ZZZ-999") is None

        print("\nasset_url:")
        print("   card  :", asset_url("https://x/001"))
        print("   logo  :", asset_url("https://x/logo", quality="", extension="png"))
        print("   empty :", repr(asset_url("")))
    finally:
        await c.close()


asyncio.run(main())
print("\nTCGDEX ASSERTIONS PASSED")

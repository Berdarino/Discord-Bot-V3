"""The three fixes: cache-backed search, loose ids, partial-refresh merge."""

import asyncio

from discord_bot_v3.cogs.pokemon import _chunk, _search_embeds
from discord_bot_v3.services.tcgdex import PokemonSet, TcgdexClient


async def main():
    c = TcgdexClient()
    try:
        await c.refresh()
        print(f"cache: {len(c.sets)} sets / {c.card_count} cards")

        print("\n== name search is scoped to TCG Pocket ==")
        hits = c.search("pikachu")
        print(
            f"   'pikachu' -> {len(hits)} (TCGdex's own name=like: returns 207 across ALL series)"
        )
        for h in hits[:6]:
            print(f"     {h.id:10} {h.name:22} {h.set_name}")
        assert hits and all(h.set_id in {s.id for s in c.sets} for h in hits)
        assert len(hits) < 30, "must not leak cards from other Pokemon TCG series"

        print("\n== exact names sort first ==")
        mixed = c.search("charizard")
        print("   ", [(h.name, h.id) for h in mixed[:5]])
        assert mixed[0].name.casefold() == "charizard"

        print("\n== case and partial matching ==")
        for q in ("PIKACHU", "pika", "zzzzz", "  "):
            print(f"   {q!r:10} -> {len(c.search(q))}")
        assert c.search("zzzzz") == [] and c.search("  ") == []

        print("\n== loose card ids resolve (TCGdex 404s on A1-1) ==")
        for raw in ("A1-001", "a1-001", "A1-1", "a1-1", " A1-94 ", "A1-999999", "garbage"):
            stub = c.find_stub(raw)
            print(f"   {raw!r:12} -> {stub.id + ' ' + stub.name if stub else None}")
        assert c.find_stub("A1-1").id == "A1-001"
        assert c.find_stub("a1-94").name == "Pikachu"
        assert c.find_stub("A1-999999") is None and c.find_stub("garbage") is None

        print("\n== partial refresh keeps what it already had ==")
        before = len(c.sets)
        fake = PokemonSet(
            id="ZZZ",
            name="Phantom",
            logo="",
            symbol="",
            release_date="2020-01-01",
            total=1,
            official=1,
            series_name="x",
            cards=[],
        )
        c._sets["ZZZ"] = fake  # pretend a set was cached earlier
        c._card_sets = {cc.id: s.id for s in c._sets.values() for cc in s.cards}
        await c.refresh()  # ZZZ is not in the live series list
        print(
            f"   sets before={before + 1} after={len(c.sets)} | ZZZ kept:",
            c.get_set("ZZZ") is not None,
        )
        assert c.get_set("ZZZ") is not None, "a set missing from a refresh must not be dropped"
        assert c.card_count > 2000

        print("\n== search result embeds ==")
        embeds = _search_embeds("pikachu", hits, _chunk(hits))
        print("   pages:", len(embeds), "| footer:", embeds[0].footer.text)
        print("   first lines:")
        for line in embeds[0].description.split("\n")[:3]:
            print("     ", line)
        assert all(len(e) <= 6000 for e in embeds)
    finally:
        await c.close()


asyncio.run(main())
print("\nTCGDEX FIX ASSERTIONS PASSED")

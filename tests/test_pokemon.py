"""Pokemon cog rendering + autocomplete, against live TCGdex data."""

import asyncio
import types

from discord_bot_v3.cogs.pokemon import (
    _CARDS_PER_PAGE,
    _MAX_CHOICES,
    Pokemon,
    _card_embed,
    _card_list_embeds,
    _chunk,
    _set_embed,
)
from discord_bot_v3.services.tcgdex import TcgdexClient


class Bot:
    def __init__(self):
        self.loop = asyncio.get_event_loop()

    async def wait_until_ready(self):
        pass


def actx(value="", options=None):
    return types.SimpleNamespace(value=value, options=options or {})


async def main():
    cog = Pokemon.__new__(Pokemon)  # skip __init__ so no task loop starts
    cog.bot = Bot()
    cog.tcgdex = TcgdexClient()
    try:
        await cog.tcgdex.refresh()
        print(f"cache: {len(cog.tcgdex.sets)} sets / {cog.tcgdex.card_count} cards")

        print("\n== set autocomplete ==")
        for typed in ("", "genetic", "A2", "zzz"):
            got = await cog.set_autocomplete(actx(typed))
            print(f"   {typed!r:10} -> {len(got):2} {[c.name for c in got[:3]]}")
        assert len(await cog.set_autocomplete(actx(""))) <= _MAX_CHOICES
        assert await cog.set_autocomplete(actx("zzz")) == []
        assert any("Genetic Apex" in c.name for c in await cog.set_autocomplete(actx("genetic")))

        print("\n== card autocomplete depends on the chosen set ==")
        got = await cog.card_autocomplete(actx("bulba", {"set": "A1"}))
        print("   set=A1 'bulba' ->", [(c.name, c.value) for c in got])
        assert got and got[0].value == "A1-001"
        by_num = await cog.card_autocomplete(actx("001", {"set": "A1"}))
        print("   set=A1 '001'   ->", [c.name for c in by_num[:3]])
        print("   no set         ->", await cog.card_autocomplete(actx("x", {})))
        assert await cog.card_autocomplete(actx("x", {})) == []
        assert await cog.card_autocomplete(actx("x", {"set": "NOPE"})) == []
        capped = await cog.card_autocomplete(actx("", {"set": "A1"}))
        print(f"   set=A1 ''      -> {len(capped)} (capped at {_MAX_CHOICES})")
        assert len(capped) == _MAX_CHOICES
        assert all(len(c.name) <= 100 for c in capped), "choice names must fit Discord's limit"

        print("\n== set embed ==")
        s = cog.tcgdex.get_set("A1")
        e = _set_embed(s, index=1, total=15)
        print("   title:", e.title, "| desc:", e.description)
        print("   image:", e.image.url)
        print("   fields:", [(f.name, f.value) for f in e.fields], "| footer:", e.footer.text)
        assert len(e) <= 6000

        print("\n== card list paging ==")
        embeds = _card_list_embeds(s, _chunk(s.cards))
        print(f"   {len(s.cards)} cards -> {len(embeds)} pages of {_CARDS_PER_PAGE}")
        print("   page 1 first lines:")
        for line in embeds[0].description.split("\n")[:3]:
            print("     ", line)
        print("   footer:", embeds[0].footer.text)
        assert len(embeds) == -(-len(s.cards) // _CARDS_PER_PAGE)
        assert all(len(e) <= 6000 for e in embeds), "a page exceeds the embed limit"

        print("\n== pokemon card embed ==")
        card = await cog.tcgdex.get_card("A1-001")
        e = _card_embed(card)
        print("   title:", e.title, "| colour:", hex(e.colour.value), "(Grass)")
        for f in e.fields:
            print(f"     {f.name:9}: {f.value[:60]!r}")
        print("   footer:", e.footer.text)
        assert e.colour.value == 0x7DB808 and e.image.url.endswith("/high.png")
        assert len(e) <= 6000

        print("\n== trainer card embed (no HP/type/attacks) ==")
        t = await cog.tcgdex.get_card("P-A-001")
        e = _card_embed(t)
        print("   fields:", [f.name for f in e.fields], "| footer:", e.footer.text)
        assert "HP" not in [f.name for f in e.fields]
        assert len(e) <= 6000

        print("\n== the biggest set stays within limits ==")
        big = max(cog.tcgdex.sets, key=lambda x: len(x.cards))
        pages_ = _card_list_embeds(big, _chunk(big.cards))
        print(
            f"   {big.name}: {len(big.cards)} cards -> {len(pages_)} pages, "
            f"max embed {max(len(e) for e in pages_)} chars"
        )
        assert max(len(e) for e in pages_) <= 6000
    finally:
        await cog.tcgdex.close()


asyncio.run(main())
print("\nPOKEMON ASSERTIONS PASSED")

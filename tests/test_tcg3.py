"""Improvements: picker menus, boosters, rarity symbols, random."""

import asyncio

from discord.ext import pages

from discord_bot_v3.cogs.pokemon import (
    _CARDS_PER_PAGE,
    CardPaginator,
    CardPicker,
    Pokemon,
    _card_embed,
    _card_list_embeds,
    _chunk,
    _rarity,
    _search_embeds,
    _set_embed,
)
from discord_bot_v3.services.tcgdex import TcgdexClient


class Bot:
    def __init__(self):
        self.loop = asyncio.get_event_loop()


async def main():
    cog = Pokemon.__new__(Pokemon)
    cog.bot = Bot()
    cog.tcgdex = TcgdexClient()
    try:
        await cog.tcgdex.refresh()

        print("== boosters are now captured ==")
        a1 = cog.tcgdex.get_set("A1")
        print("   A1 packs:", a1.boosters)
        assert a1.boosters == ["Mewtwo", "Charizard", "Pikachu"]
        e = _set_embed(a1, index=1, total=15)
        print("   set embed fields:", [(f.name, f.value[:40]) for f in e.fields])
        assert any(f.name == "Packs" for f in e.fields)

        card = await cog.tcgdex.get_card("A1-036")  # Charizard ex
        print("   A1-036 found in:", card.boosters)
        assert card.boosters == ["Charizard"]
        ce = _card_embed(card)
        assert any(f.name == "Found in" for f in ce.fields)
        print("   card embed fields:", [f.name for f in ce.fields])

        print("\n== rarity symbols ==")
        for r in ("One Diamond", "Four Diamond", "Two Star", "Crown", "Weird New Rarity", ""):
            print(f"   {r!r:20} -> {_rarity(r)!r}")
        assert _rarity("Two Star") == "★★ Two Star"
        assert _rarity("Weird New Rarity") == "Weird New Rarity"  # degrade, don't hide
        assert _rarity("") == "—"

        print("\n== picker menu is attached per page ==")
        chunks = _chunk(a1.cards)
        pickers = [CardPicker(cog, c) for c in chunks]
        print(f"   {len(a1.cards)} cards -> {len(chunks)} pages, {len(pickers)} pickers")
        sel = pickers[0].children[0]
        print(
            "   select options:",
            len(sel.options),
            "| first:",
            (sel.options[0].label, sel.options[0].value, sel.options[0].description),
        )
        assert len(sel.options) == _CARDS_PER_PAGE <= 25, "Discord caps select options at 25"
        assert all(len(o.label) <= 100 for o in sel.options)
        assert sel.options[0].value == a1.cards[0].id

        print("\n== paginator merges the select with its buttons ==")
        built = [
            pages.Page(embeds=[e], custom_view=pickers[i])
            for i, e in enumerate(_card_list_embeds(a1, chunks))
        ]
        p = CardPaginator(pages=built, show_indicator=True, author_check=True)
        # Mirror respond(): nav buttons claim row 0 first, then the page's view.
        p.update_buttons()
        for btn in p.buttons.values():
            item = btn["object"]
            if item not in p.children:
                p.add_item(item)
        p.update_custom_view(built[0].custom_view)
        kinds = [type(i).__name__ for i in p.children]
        print("   paginator children:", kinds)
        assert "Select" in kinds, "picker must sit alongside the nav buttons"
        assert sum("Button" in k for k in kinds) >= 2, "nav buttons must survive the merge"

        print("   flipping through every page must not stack selects")
        for i in range(1, len(built)):
            p.update_custom_view(built[i].custom_view)
        kinds2 = [type(i).__name__ for i in p.children]
        selects = [i for i in p.children if type(i).__name__ == "Select"]
        print("   after switch:", kinds2)
        assert len(selects) == 1, "the old page's select must be removed"
        assert selects[0].options[0].value == chunks[-1][0].id

        print("\n== search results carry a picker too ==")
        hits = cog.tcgdex.search("pikachu")
        hchunks = _chunk(hits)
        hembeds = _search_embeds("pikachu", hits, hchunks)
        print(f"   {len(hits)} hits -> {len(hembeds)} page(s)")
        hp = CardPicker(cog, hchunks[0])
        print("   options:", [(o.label, o.description) for o in hp.children[0].options[:3]])
        assert len(hp.children[0].options) == len(hits)

        print("\n== random card ==")
        picks = {cog.tcgdex.random_stub().id for _ in range(20)}
        print(f"   20 draws -> {len(picks)} distinct ids, e.g. {sorted(picks)[:4]}")
        assert len(picks) > 10, "random must actually vary"
        assert all(cog.tcgdex.find_stub(i) for i in picks)
    finally:
        await cog.tcgdex.close()


asyncio.run(main())
print("\nPOKEMON IMPROVEMENT ASSERTIONS PASSED")

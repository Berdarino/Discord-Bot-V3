# Working on this repo

`README.md` explains **what** the bot does and why each feature is shaped the
way it is. This file covers **what will bite you**. Read both before changing
anything non-trivial.

## Stack

Python 3.14, [Pycord](https://docs.pycord.dev) 2.8, `uv`, `src/` layout, ruff.
MariaDB and Redis on localhost in dev and on the production host.

```bash
uv sync                              # install, including dev deps
cp .env.example .env                 # then fill it in -- see below
uv run python -m discord_bot_v3      # run the bot

uv run python tests/run.py           # all 18 tests
uv run python tests/run.py --offline # the 6 needing no services
uv run python tests/run.py test_media  # one test, full output

uv run ruff check . && uv run ruff format .
```

`.env` is gitignored and holds real credentials (bot token, KLIPY key, MAL
client id, database password). Never commit it, never echo its values into
logs, error messages or test output. `.env.example` documents every key.

## Landmines

**Ruff rewrites `except (A, B):` into PEP 758 `except A, B:`.** Python 3.14
allows the unparenthesised form and the formatter prefers it. Any patch that
matches on the parenthesised spelling therefore matches nothing and **fails
silently** — the file is left untouched while the tool reports success. This
has happened four times in this repo. Ruff also reflows long calls onto one
line, breaking multi-line anchors the same way. Assert your anchor exists
before replacing, or edit by line position, and re-read a file after
`ruff format` before patching it again.

**Never add `from __future__ import annotations` to a cog module.** Pycord
reads the `discord.Option` objects out of command signatures at runtime. PEP
563 stringifies them and every option silently degrades to a required string —
no error, just a broken command. The `FA` and `TC` ruff rules are disabled for
exactly this reason; see the comment in `pyproject.toml`. Service modules are
fine, and most of them do use it.

**Pycord is not discord.py.** `setup()` and `add_cog()` are *synchronous*.
There is no `setup_hook`. `on_ready` fires again on every reconnect, so
one-time startup work belongs in `Bot.start`, not there.

**Modals are for composition, never discovery.** A modal cannot reach an API
while open: `InputText` has no autocomplete parameter, `Select` options are
fixed at construction, a modal cannot follow a `defer()`, and a modal cannot
answer another modal's submit. Use one for input the user already knows
(`/send`'s multi-line body). Use slash-option **autocomplete** for anything
where the API should suggest as you type. This was considered and settled for
`/anime`, `/manga` and `/pokemon` — don't reopen it without new information.

**A component added to a paginator needs an explicit `row=`.** Row 0 belongs
to the nav buttons, and a collision raises `item would not fit at row 0`.
Pycord's `update_custom_view` also fails to remove a `Page`-level view, so
selects accumulate and the *second* page flip raises; `CardPaginator` in
`cogs/pokemon.py` exists to work around that.

## Deliberate decisions — do not "fix" these

**AniList returns HTTP 403 to this client** ("temporarily disabled due to
severe stability issues"). The block lifts if you send
`Referer: https://anilist.co`. We deliberately do **not**, because it would
misrepresent the bot as AniList's own website to bypass an access control the
operator put there on purpose. MyAnimeList is the fallback and in practice
answers everything today. Adding that header is the single most likely
"helpful" regression here.

**The bot owner is whoever owns the application in the Discord Developer
Portal.** There is no `OWNER_ID` env var and there should not be one.

**Tables are created per feature, on demand.** `Database.ensure_schema` runs a
feature's own DDL when its cog loads; there is no central migration file and no
pre-baked schema. Add a `SCHEMA` constant beside the feature that needs it.

**`DISCORD_BOT_V2_DOCUMENTATION.md` is reference only, and contains errors.**
It is the previous bot's docs, used to decide what to port. It is not a spec,
and it has been wrong before: line 654 documents `dob` as a `VARCHAR` holding
`MM-DD`, when the real column is a `DATE` and the MM-DD form is a generated
column derived from it. Verify against reality before trusting it.

## Conventions

**Database.** MariaDB, not MySQL — use `utf8mb4_unicode_ci`, never
`utf8mb4_0900_*`. Snowflakes are `BIGINT UNSIGNED`. Times are stored naive UTC.
Generated columns must be deterministic, so `CONCAT(LPAD(MONTH(...)))` rather
than `DATE_FORMAT`. Never name a column `datetime`.

**Storage split.** MariaDB holds anything the bot would be sad to lose. Redis
only caches derived data. Both fail soft: unset or unreachable, every call
no-ops and the feature still works, just slower. Keep it that way — a cache
that can take the bot down is worse than no cache.

**Verify APIs against reality.** Every quirk documented here and in the README
was found by probing a live endpoint, not by reading docs; several of the docs
were wrong or unreadable. If an endpoint is gated from your sandbox, ask
Bernard to run the request through Postman rather than guessing the shape.

**Interaction design.** Prefer Discord's native components over plain text
replies: buttons, select menus, autocomplete, context menus, ephemeral
responses. Confirm before anything irreversible. Say what the tradeoff is
(extra state, view timeouts, nothing survives a restart) rather than only the
upside.

## State of play

13 commands across 6 cogs: `General`, `Gifs`, `MediaSearch`, `Owner`,
`Pokemon`, `Reminders`. All 18 tests pass; ruff is clean.

Not done yet: **nothing has been run against a real Discord gateway.** Every
test drives the objects directly or hits the third-party APIs. Ported from V2
so far are `/send`, `/delete`, `/gif`, `/anime`, `/manga`, `/pokemon` and
reminders. Still to port: members/birthdays, quotes, LLM chat (Gemini), and the
event handlers (`on_message_edit`, `on_message_delete`, `on_member_join`) with
the birthday task.

One known gap: `AniListClient.details()` has never run against the live API,
because AniList has been 403 throughout. Its parser is unit-tested and every
lookup tolerates a missing branch, so a shape surprise degrades to an empty
section rather than an exception — but it is unverified.

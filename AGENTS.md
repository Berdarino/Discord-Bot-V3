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

uv run python tests/run.py           # all 21 tests
uv run python tests/run.py --offline # the 9 needing no services
uv run python tests/run.py test_media  # one test, full output

# The mariadb tests use <MYSQL_DB>_test, not MYSQL_DB. Never point them at live data.

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

**The database tests run against `<MYSQL_DB>_test`, never the live database.**
They create, fill and `DROP` their tables, so pointing them at `MYSQL_DB` means
a suite run silently wipes real reminders — which is exactly what used to
happen. `Database.connect` creates a database that does not exist, so the
`_test` sibling needs no setup. Any new test that touches MySQL must use
`TEST_DB_SUFFIX` too.

**Qwen3 emits `<think>` blocks and `think: false` does not always stop it.**
Reported broken across several Qwen3 builds, so `services/ollama.py` strips the
tags as well — closed, orphaned (a reply truncated by `num_predict` keeps the
closing tag only) and unclosed. Remove the stripper and the bot posts its own
reasoning to the channel as its reply.

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

**AniList is answering again (checked 2026-09-14).** It spent a long stretch
returning HTTP 403 ("temporarily disabled due to severe stability issues"); it
no longer does, and it is the primary provider again. If it 403s once more, the
block lifts by sending `Referer: https://anilist.co` — we deliberately do
**not**, because it would misrepresent the bot as AniList's own website to
bypass an access control the operator put there on purpose. Adding that header
is the single most likely "helpful" regression here.

**AniList's title matching breaks on partial words, so an empty result falls
through to MyAnimeList.** `naru` and `naruto` each return ten; `narut` returns
nothing. `frier` returns nothing while MAL returns five. Autocomplete types one
character at a time and lands on those gaps constantly, which silently emptied
it when AniList came back. `_fetch` therefore treats an empty AniList answer to
a *title search* as "ask MAL", while an empty **browse** (no search term) is
taken at face value. Do not "simplify" that back into a single return.

**Chat runs on a local model, deliberately.** V2 used Gemini via LangChain.
V3 uses Ollama over plain aiohttp — no SDK, no new dependency, no API key, and
nothing said in the server leaves the machine. That last point is the reason:
the alternative free tier trains on what you send it and says so in its terms,
and these are real people's private messages. The cost is quality — an 8B model
is weaker at the persona's Sarawak Chinese than a hosted model would be.

**The character is `PERSONA` in `services/chat.py`, not a setting.** It is
prose the model is shown verbatim, so do not reflow it to 100 columns and do not
let ruff normalise fullwidth punctuation in it — there are per-file `E501` and
`RUF001` ignores for exactly that. Per-person handling comes from
`members.description`, appended by `build_system`.

**Two columns are curated by hand: `members.birthday` and
`members.description`.** No command sets either, deliberately — a row edit beats
a command plus validation for one small server, and it keeps both to whoever has
database access. Do not "helpfully" add `/birthday set` back.

**`CREATE TABLE IF NOT EXISTS` will not add a column to a table that already
exists.** A feature that grows a column must also call
`Database.ensure_column`, or every database created before the change silently
keeps the old shape. `MemberStore.ADDED_COLUMNS` is the worked example. The
check goes through `information_schema` because `ADD COLUMN IF NOT EXISTS` is
MariaDB-only.

**Replayed history beats the system prompt, so language is enforced twice.**
Measured on qwen3:8b: asked in English with no history it answers in Chinese
3/3; a "reply in English" line last in the system prompt fixes it 0/3; replay a
Chinese history and the same line loses 3/3. Code-switched input is worse still: one
Chinese word in an English sentence leaks 4/10 with the system pin alone, and
1/10 once the same instruction is repeated on the user turn (`pin_language`).

`detect_language` is therefore applied three ways — the system prompt, the user
turn, and filtering the replayed history — and all three are load-bearing.
Dropping any one brings the bug back. Telling the model to *mirror* a mix was
measured and is worse than forcing one language: full Chinese every time, with
degenerate repetition. Re-measure rather than reasoning about it if the model
changes.

`pin_language` applies to the request only; `remember` stores the clean
message, or the reminder accumulates through the history.

**A local model serves one generation at a time.** `cogs/chat.py` holds a lock
across the whole call. Removing it does not make replies arrive sooner; it makes
all of them arrive late.

**The bot owner is whoever owns the application in the Discord Developer
Portal.** There is no `OWNER_ID` env var and there should not be one.

**The message edit/delete log goes to `LOG_CHANNEL_ID`, not to a DM.** V2 DMed
the owner; this was changed deliberately, because the point of the feature is a
readable, searchable record of who edited and deleted what. The channel is
resolved once and cached, and any permanent failure (missing, invisible, not a
guild text channel, cannot post) disables logging instead of failing on every
event. Events in the log channel itself are skipped, or tidying the log would
write more log.

**`message_content` is a hard startup dependency now.** The members intent is
merely withheld when it is not ticked in the Developer Portal; the message
content intent makes Discord **refuse the gateway connection**, so a missing
tick is a bot that will not start, not a feature that quietly degrades.

**Two sources describe the same event, and merging them naively double-logs
everything.** Discord sends a specific gateway event (*what* happened) *and* an
audit log entry (*who* did it) for most moderator actions. `serverlog.py`
resolves this with `_AUDIT_HANDLED`: six actions belong to dedicated handlers
that know more than the audit entry does, and `on_audit_log_entry` skips exactly
those and renders everything else generically. Adding a handler for an action
means adding it to that set, or it is logged twice; removing one means removing
it, or it stops being logged at all.

**`MESSAGE_CACHE` in `bot.py` is what decides how much the log can show.**
Only messages still in Pycord's cache have a `before` to diff or content to
quote; everything else degrades to an `on_raw_*` event. Raising it is the one
lever that widens the window.

**`on_message_edit` fires without an edit.** A link unfurling into an embed, or
a pin, dispatches the same event. Compare `before.content` to `after.content`
before doing anything. The raw form has no `before` to compare, so it keys off
`edited_timestamp` being present in the payload instead.

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

14 commands across 9 cogs: `Chat`, `General`, `Gifs`, `MediaSearch`,
`Members`, `Owner`, `Pokemon`, `Reminders`, `ServerLog`. All 21 tests pass;
ruff is clean.

Neither `General` nor `ServerLog` owns a command. They are split by audience:
`General` posts the public welcome to the guild's system channel, `ServerLog`
writes ten listeners' worth of member, message and moderation activity to
`LOG_CHANNEL_ID` and does not load without it. Three cogs listen to
`on_member_join` — one greets, one logs, one writes the database row — and the
split is deliberate, so each works when the other two are off.

Departed members keep their `members` row, so a rejoin does not lose a birthday.
That makes the table not a guest list, and the announcer therefore checks
`guild.get_member` before posting; without that check it pings people who left.

The bot has been run against a real gateway and exercised by hand on a test
server; the suite itself still drives objects directly or hits third-party APIs
rather than connecting. Ported from V2 so far are `/send`, `/delete`, `/gif`, `/anime`, `/manga`, `/pokemon`,
reminders, members/birthdays, the event handlers and LLM chat. **Every V2
feature is now ported.**

What is left of the chat port is the *per-member* half: V2's `role_message`
column and `QUOTE_TABLE` let each person customise the bot's voice toward them.
`build_system()` already accepts both; nothing writes them. V2 had no command to
manage them either, so porting that usefully means designing one.

`AniListClient.details()` was unverified for a long time because of the 403.
It has now been run against the live API (2026-09-14) and all five fields it
parses come back populated: relations, recommendations, links, stats and staff.
That gap is closed.

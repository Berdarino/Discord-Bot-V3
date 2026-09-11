# Discord Bot V3

A Discord bot built with [Pycord](https://docs.pycord.dev/) 2.8 on Python 3.14, managed with [uv](https://docs.astral.sh/uv/).

## Setup

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications),
   add a **Bot**, and copy its token.
2. Configure the environment:

   ```bash
   cp .env.example .env
   # then edit .env and paste your token into DISCORD_TOKEN
   ```

3. Invite the bot to a server. Under **OAuth2 → URL Generator**, select the
   `bot` and `applications.commands` scopes, then open the generated URL.
4. Put your server's id in `GUILD_IDS` (enable Developer Mode in Discord, then
   right-click the server → Copy Server ID). Commands appear instantly in those
   guilds; leaving it empty registers globally and can take up to an hour.

## Running

### Local development — uv

```bash
uv run python -m discord_bot_v3
```

uv creates and syncs the virtualenv automatically; there is no venv to activate.
Code changes take effect on the next start — no rebuild step.

Handy during development:

```bash
uv sync                      # after pulling changes to pyproject.toml / uv.lock
uv add <package>             # add a dependency
LOG_LEVEL=DEBUG uv run python -m discord_bot_v3   # verbose, logs every HTTP call
```

### Linting and formatting

[Ruff](https://docs.astral.sh/ruff/) is a dev dependency, so it is installed by
`uv sync` locally but excluded from the Docker image (`uv sync --no-dev`).

```bash
uv run ruff check .           # lint
uv run ruff check --fix .     # lint and autofix
uv run ruff format .          # format
```

Two rule families are deliberately **not** enabled, and the reason is in
`pyproject.toml` next to the config:

- `FA` (flake8-future-annotations) would add `from __future__ import annotations`
  to cog modules, which silently breaks every slash command option.
- `TC` (flake8-type-checking) would move runtime-needed imports into
  `if TYPE_CHECKING:` blocks, breaking the same introspection.

If you ever widen the rule set, re-run the bot afterwards and confirm options
still report their real types — this class of breakage is invisible to the
linter and to startup, and only shows up in Discord's UI.

### Deployment — Docker Compose

```bash
docker compose up -d --build    # build and start in the background
docker compose logs -f          # follow logs
docker compose down             # stop and remove
```

`.env` is read at runtime via `env_file` and is excluded from the image by
`.dockerignore`, so no token is ever baked into a layer. Code changes need a
rebuild: `docker compose up -d --build`.

### Do not run both at once

Both paths read the same `DISCORD_TOKEN`. Running the container and a local
`uv run` simultaneously opens two gateway sessions for the same bot, and **every
slash command gets answered twice**. Stop one before starting the other:

```bash
docker compose down          # before working locally
```

Discord permits the two sessions, so there is no error to warn you — the only
symptom is duplicate replies.

## Layout

```
Dockerfile       two-stage build; runtime image carries only the venv
compose.yaml     service definition, restart policy, log rotation
.dockerignore    keeps .env and .venv out of the build context

src/discord_bot_v3/
├── __main__.py     entry point: config, logging, startup, exit codes
├── bot.py          Bot subclass, cog discovery, global error handling
├── config.py       environment parsing and validation
├── ui.py           reusable components (ConfirmView)
├── cogs/           one module per feature — auto-discovered at startup
│   ├── general.py  general event listeners
│   ├── members.py  /birthday, member sync, daily birthday task
│   ├── gif.py      /gif — needs KLIPY_API_KEY
│   ├── media.py    /anime, /manga
│   ├── pokemon.py  /pokemon — TCG Pocket sets and cards
│   ├── reminders.py  remind-me context menus + scheduler
│   └── owner.py    /send, /delete — owner only
└── services/       third-party API clients, imported explicitly
    ├── klipy.py    KLIPY GIF search
    ├── media.py    provider-neutral Media model
    ├── anilist.py  AniList GraphQL (primary)
    ├── mal.py      MyAnimeList API v2 (fallback)
    ├── tcgdex.py   TCGdex — Pokémon TCG Pocket
    ├── database.py MySQL/MariaDB pool (no schema of its own)
    ├── cache.py    Redis cache (optional, best-effort)
    └── reminders.py reminders table + queries
```

## Commands

| Command | Who | What |
|---------|-----|------|
| `/gif` | everyone | Preview a GIF from KLIPY privately, shuffle, then post it. |
| `/anime` | everyone | Search anime, with title autocomplete. AniList, falling back to MyAnimeList. |
| `/manga` | everyone | Search manga, with title autocomplete. AniList, falling back to MyAnimeList. |
| `/pokemon sets list / get` | everyone | Browse Pokémon TCG Pocket sets. |
| `/pokemon cards …` | everyone | `list`, `search`, `get`, `random`, `id` over TCG Pocket cards. |
| `/pokemon update` | everyone | Refresh the cached TCGdex data (5 min cooldown). |
| `/birthday set` | everyone | Save your birthday privately for this server. |
| `/birthday remove` | everyone | Remove your saved birthday from this server. |
| **Remind me in…** | everyone | Right-click a message → Apps. Reminder after an offset. |
| **Remind me at…** | everyone | Right-click a message → Apps. Reminder at a wall-clock time. |
| `/reminders` | everyone | List your pending reminders and cancel them. |
| `/send` | owner | Pick a channel and compose a message in one modal, posted as the bot. |
| `/delete` | owner | Bulk delete the last 1–100 messages in the current channel. |
| **Delete up to here** | owner | Right-click a message → Apps. Deletes it and everything after it. |

### Owner-only commands

`/send` and `/delete` are gated by `commands.is_owner()`. The owner is whoever
owns the application in the [Developer Portal](https://discord.com/developers/applications) —
Pycord fetches that on the first check and caches it, so there is nothing to
configure. If the application belongs to a team, every team member counts as an
owner. Transfer the application to change who that is.

Discord has no notion of "bot owner", so it cannot hide these on its own. They
are declared with `default_member_permissions` requiring **Administrator**,
which keeps them out of the command list for ordinary members. That is
presentation only — `is_owner()` is what actually refuses them, and a non-owner
admin who can see one still gets an ephemeral refusal.

Two consequences worth knowing: a server admin can override the requirement per
role or channel in **Server Settings → Integrations**, and if you are ever not
an Administrator in a server, you will not see your own owner commands there.

Both go through Discord's own UI rather than answering in one shot:

- `/send` takes **no options at all**. It opens a **modal** holding both the
  channel picker and the message body, so nothing is chosen while typing the
  command. This needs `discord.ui.DesignerModal` — the legacy `discord.ui.Modal`
  accepts text inputs only. The modal is also the only way to write a multi-line
  message: slash-command string options are single-line inputs. Submitting the
  modal sends immediately; the reply is ephemeral.
  The picker covers text, announcement, and voice channels (voice channels carry
  a built-in text chat and are `Messageable` like any other).
- `/delete` asks for **Delete / Cancel** before purging, because there is no undo.
- **Delete up to here** is a *message context menu* (right-click a message →
  Apps), not a slash command. Pick where a mess started instead of counting
  messages by eye. It counts first, shows the number in the confirmation, and
  refuses outright above 100 rather than silently truncating.
  That prompt is ephemeral, only clickable by whoever opened it, and expires
  after 120 seconds by disabling its own buttons. Views are not persistent, so a
  restart leaves an open prompt inert — deliberate for a confirmation, which
  should fail closed.

`/delete` needs the bot to hold **Manage Messages** and **Read Message History**
in the channel, and Discord refuses to bulk delete anything older than 14 days.
Its summary tallies deletions per author rather than quoting the messages:
reading message text needs the privileged `message_content` intent, which this
bot does not request.

### `/gif` and KLIPY

GIFs come from [KLIPY](https://klipy.com), not Tenor — Tenor's public API is
being sundowned. Get a key at [partner.klipy.com](https://partner.klipy.com) and
put it in `.env` as `KLIPY_API_KEY`.

The key is **optional**. Without it the bot starts normally, logs a warning, and
simply does not register `/gif`.

Two things about KLIPY's API differ from Tenor's and are easy to trip over:

- The API key is a **path segment**, not a query parameter:
  `/api/v1/{key}/gifs/search`. It must stay out of logs and error messages.
- Responses are wrapped in a `{"result": bool, "data": {...}}` envelope and the
  result list sits at `data.data` — one level deeper than it looks.

`/gif` asks KLIPY for the strictest content filter (`high`) everywhere except
channels Discord marks age-restricted, where it relaxes to `low`.

The result is previewed **privately** with **Shuffle / Post / Cancel** buttons,
so nothing reaches the channel until you pick it. The whole result page is
already in memory, so shuffling costs no further API calls — and the results are
shuffled once then walked in order, rather than re-rolled each press, so you
cannot see the same GIF twice before seeing all of them. Shuffle is disabled
outright when the search returned a single result. Posting removes the private
preview entirely rather than replacing it with a receipt. The preview expires
after 120 seconds without posting anything.

### `/anime` and `/manga`

**Autocomplete is the fast path.** Type three characters into `search` and the
bot suggests real titles, disambiguated by format and year
(`Frieren: Beyond Journey's End (TV, 2023)`). Picking one returns that exact
title as a single embed — no paging, no guessing which result is the right one.
A picked suggestion travels as `provider:id` rather than as text, so the command
fetches the title directly instead of searching for its name again.

Typing free text and pressing enter still searches normally. Results are one
embed per title, browsed with Pycord's paginator — Prev/Next locked to whoever
ran the command, auto-disabling after 5 minutes. Each embed is tinted with the
cover art's own dominant colour. Bare `/anime` shows the current season; bare
`/manga` shows the current year.

**Results are re-ranked locally.** Neither provider puts the obvious answer
first: MAL returns `Cowboy Bebop: The Movie` above `Cowboy Bebop`, and `Frieren`
Season 2 above Season 1. `rank_by_relevance` sorts exact title matches first,
then prefixes, then substrings, breaking ties by popularity. Nothing is ever
dropped — only reordered. AniList additionally gets `sort: SEARCH_MATCH` when a
search term is present, instead of `POPULARITY_DESC`, which ranks a popular
spin-off above the title you asked for.

**Currently airing shows say when the next episode lands**, rendered as a
Discord relative timestamp so every viewer sees it in their own timezone.
AniList gives an exact episode number and airing time; MAL only publishes a
weekly broadcast slot, which is projected forward at render time. MAL keeps that
slot on *finished* shows too — Cowboy Bebop still reports "friday" — so the
projection only applies while a title is actually `RELEASING`.

**Details** is a button on every result. It opens an ephemeral panel with
related titles (sequels, prequels, spin-offs), recommendations, staff credits
and list statistics; from AniList it also carries streaming links, which MAL
does not expose. The button reads the paginator's current page rather than being
rebuilt per page, which is what caused components to accumulate in `/pokemon`.

Every filter is a **choice list** rather than free text, so there is no guessing
between `LIGHT_NOVEL` and "light novel". Adult titles are gated on the channel
being Discord-age-restricted rather than offered as a filter, and `Hentai` is
left out of the genre list.

**Two providers, one model.** `services/media.py` defines a provider-neutral
`Media`; AniList and MyAnimeList both map onto it, so the embed builder never
knows which answered. AniList is primary. When it refuses (see below) the search
silently retries against MAL and the footer says `MyAnimeList` instead of
`AniList` — the switch is not announced separately, because the footer is
already the answer.

MAL needs a client id: register an app at
[myanimelist.net/apiconfig](https://myanimelist.net/apiconfig) and put it in
`.env` as `MAL_CLIENT_ID`. Without it AniList still works; only the fallback is
lost, and the commands say so.

**MAL's search barely filters.** `GET /anime` takes only `q`, `limit`, `offset`
and `fields` — no genre, format, status, source or date parameters. Rather than
drop those filters, `services/mal.py` requests a large candidate page and
narrows it **locally**, since every field needed is already in the response.
The cost is that a narrow filter over a broad query can come back thin; it never
comes back wrong.

Where MAL has a better endpoint than free-text search, it is used:

| Request | Endpoint |
|---|---|
| Search text (3+ chars) | `/anime?q=` |
| Season + year | `/anime/season/{year}/{season}` |
| No filters at all | `/anime/ranking?ranking_type=bypopularity` |

Only two things MAL genuinely cannot do: **country of origin** (it has no such
field) and **`TV_SHORT`** (not in its vocabulary). Those *are* called out in a
short ephemeral note, because nothing in the embed would reveal it.

**On AniList's 403.** AniList answers some clients with `HTTP 403` and "The
AniList API has been temporarily disabled due to severe stability issues." The
block lifts if you send `Referer: https://anilist.co`, which would misrepresent
the bot as their own website — so the client deliberately does not, and falls
back to MyAnimeList instead.

Because that refusal is a standing condition rather than a blip, a 403 **opens a
circuit breaker** in Redis for 30 minutes: subsequent searches skip AniList
entirely instead of paying a failed round trip each time, and the key's
expiry is what re-probes for recovery — no restart needed. Two failures are
deliberately excluded from this: a **rate limit** (`429`) propagates to the
caller untouched, since a backoff is not an outage and switching provider would
only hide that you should wait; and an ordinary **network error** falls back for
that one call without opening the breaker, since it may be a passing fault.

The breaker is also part of the cache key, so results fetched while AniList was
down are not still being served once it recovers. Without Redis the breaker
simply never opens and behaviour is exactly as it was before.

### `/pokemon`

Browses the Pokémon TCG Pocket pool from [TCGdex](https://tcgdex.dev) — no key
needed. A command group with two subgroups:

```
/pokemon update              refresh the cache now
/pokemon sets list           every set, one embed each
/pokemon sets get   <set>    one set
/pokemon cards list   <set>   a set's cards, 20 per page
/pokemon cards search <name>  by name, across every set
/pokemon cards get    <set> <card>
/pokemon cards random         a random card
/pokemon cards id     <id>    e.g. A1-001 or A1-1
```

`set` and `card` **autocomplete**, and `card` narrows to whichever set is
already selected in the same command. That is why the set list is held in
memory: autocomplete must answer within Discord's 3 second window, so it cannot
make a round trip. The cache is built at startup and rebuilt every 24 hours;
`/pokemon update` forces it in between and is cooldown-limited, since it refetches
every set. Full card details are fetched on demand rather than cached — the
series holds ~2,400 cards and only a few are ever looked at.

One deliberate difference from V2: `/pokemon cards list` shows a **compact text
listing** rather than one embed per card. A 331-card set would otherwise be 331
pages; `/pokemon cards get` is there when you want the artwork. Card embeds are
tinted by energy type.

Every card listing carries a **select menu** next to the paginator buttons, so
you open a card straight from the list instead of copying an id into another
command. Set embeds show which **packs** a set opens as (A1 is Mewtwo /
Charizard / Pikachu), card embeds show which pack a card is pulled from, and
rarities render the way the game draws them (`◆◆◆`, `★★`, `♛`) while still
printing the name, so an unrecognised new rarity degrades instead of vanishing.

Two Pycord traps that per-page views walk straight into, both fixed here:

- A select is full width and the paginator's navigation already occupies row 0,
  so a picker **must** set `row=` explicitly or `add_item` raises
  `item would not fit at row 0`.
- `Paginator.update_custom_view` only removes items belonging to the
  *paginator-level* `custom_view` — a view attached to a `Page` is never taken
  off again, so selects accumulate and the **second page flip** raises
  `item would not fit at row 1`. `CardPaginator` remembers the view it applied
  last so the base class can clear it.

**Search runs against the cache, not TCGdex's search.** TCGdex does support
`?name=like:pikachu`, but its `serie` filter does not work on `/cards`, so that
query returns 200+ cards from every Pokémon TCG set ever printed — wrong for a
TCG Pocket bot. The local cache gives the correct 16, instantly, with no request.
A single match skips the list and shows the card directly.

Two other traps worth recording, both verified against the live API:

- TCGdex's `?set=` filter is a **prefix match** — `set=A1` returns 372 cards,
  including every `A1a` card. Not used here for that reason.
- Card ids are case-insensitive but **width-sensitive**: `A1-001` resolves and
  `A1-1` is a 404. `/pokemon cards id` resolves through the cache first, so the
  short form people actually type works.

### Reminders

Right-click any message → **Apps** → *Remind me in…* (offset) or *Remind me at…*
(wall clock). Both open a modal with an optional note. A context menu rather
than a slash command because a reminder is always *about* a message — and it
hands us the jump URL for free. `/reminders` lists yours with a select menu to
cancel; it is built fresh per call, so it keeps working across restarts without
a persistent view.

Three deliberate differences from V2:

- **V2 fired on `reminder.datetime == now`, polling every second.** An exact
  second match means a tick that arrives late deletes the reminder *without
  notifying*. This uses `remind_at <= now`, so a reminder that came due while
  the bot was down fires on the next tick instead of vanishing.
- **Polling is every 5 seconds, not every second.** It is one indexed query, and
  nobody notices five seconds on a reminder.
- **Times are stored UTC**, not in one hardcoded zone. `TIMEZONE` (default
  `Asia/Kuala_Lumpur`) only says how to read bare wall-clock input; confirmations
  use Discord's own timestamp markup, so each reader sees their own zone.

Delivery replies to the confirmation message when it still exists, so the
reminder lands in context. A reminder that cannot be delivered is dropped rather
than retried forever on every tick.

### Birthdays

`/birthday set` saves your own date of birth privately, per server; `/birthday
remove` deletes it. The bot records member names when it starts and when someone
joins, but it never imports birthdays from Discord because Discord does not
provide them. A birthday is stored as a real date, while announcements use only
its month and day, never revealing a person's age or birth year.

Set `BIRTHDAY_CHANNEL_ID` to the channel where announcements should appear. At
midnight in `TIMEZONE`, the bot posts one greeting for each birthday belonging
to that channel's server. Without the setting, birthdays can still be saved but
no announcement is posted. Each greeting includes the [birthday chicken GIF](https://klipy.com/gifs/happy-birthday-chicken)
from KLIPY; no media asset is stored in the repository. February 29 birthdays
are celebrated on February 28 in non-leap years.

The feature needs the **Server Members Intent** enabled under the bot's
*Privileged Gateway Intents* in the Discord Developer Portal. The bot requests
it in code; Discord will withhold member events unless it is also enabled there.

## Storage: what goes where

Two stores, with a clear split.

**MariaDB holds anything the bot would be sad to lose** — reminders and member
birthdays today, quotes later. **Redis holds only derived data**; losing it costs a
few API calls and nothing else.

| Data | Store | Why |
|---|---|---|
| Reminders | MariaDB | Must survive anything. |
| Member birthdays | MariaDB | Opt-in data and daily lookup must survive restarts. |
| Pokémon set/card index | Redis, 26h | Rebuildable, but a cold start is 16 requests. |
| Anime/manga search results | Redis, 15 min | Rebuildable; AniList rate-limits. |
| Paginators, pickers, confirmations | memory | Meaningless after a restart. |

Both are optional and fail soft. With `REDIS_URL` unset — or Redis down — every
cache call quietly no-ops and the bot fetches live; the failure is logged once,
not on every miss. A cache that takes the bot down when it fails is worse than
no cache.

The `dbv3:v1:` key prefix namespaces the bot and versions the format: bump the
version and every old key is simply never read again.

### What Redis actually buys

- **Pokémon autocomplete works immediately after a restart.** The set list is
  mirrored to Redis, so a fresh process restores 15 sets and 2,480 cards with
  **zero HTTP requests** instead of waiting on 16.
- **Repeated searches are free.** Measured: a cold `/anime Cowboy Bebop` takes
  ~1200 ms and one upstream call; the same search again takes **3 ms and none**.
  This matters far more now that autocomplete fires on keystrokes: typing
  `fri` → `frie` → `frier` measured 604 / 384 / 319 ms, all well inside
  Discord's 3-second autocomplete deadline, and repeats are served from Redis.
- **AniList outages are remembered**, not rediscovered on every search — see
  the circuit breaker under `/anime`.
- **Picked titles and Details panels are cached too** (15 minutes and 6 hours
  respectively), so pressing Details twice costs one upstream call.

One trap worth recording: `Media` nests `FuzzyDate` dataclasses, and
`dataclasses.asdict` flattens them to plain dicts. Reading one back with
`Media(**data)` would leave `start_date` a dict and make `media.start_date.year`
an `AttributeError`. `services/media.py` provides `to_cacheable` /
`from_cacheable` for exactly this reason — use them rather than `asdict`.

## Persistence

Features that need to remember things use MySQL (the dev and production hosts
both run **MariaDB**, which is wire-compatible). Point `MYSQL_*` at a server and
the bot creates its own **database** on start — no manual SQL to deploy.

It creates **no tables**. Each feature owns its schema and calls
`db.ensure_schema(...)` with its own `CREATE TABLE IF NOT EXISTS` as the cog
loads, so a table appears only once something actually needs it.

MySQL is optional. Leave `MYSQL_USER` empty and the bot runs exactly as it does
without it; if the server is unreachable at startup it logs that and carries on,
rather than refusing to boot. `bot.db` is `None` in both cases, and cogs needing
persistence check it before loading.

Conventions for feature schemas, so they stay consistent:

- Discord snowflakes are `BIGINT UNSIGNED`, never `VARCHAR`.
- Times are stored **UTC** and converted for display — the server's `time_zone`
  is `SYSTEM` and is not to be trusted.
- `utf8mb4` / `utf8mb4_unicode_ci`. The `utf8mb4_0900_*` collations are
  MySQL-only and will not load on MariaDB.
- Never name a column `datetime` — it is a type name, and needs quoting in every
  query that touches it.
- Stored generated columns must be deterministic. Build a month-day key with
  `MONTH`/`DAY` and `LPAD`, not `DATE_FORMAT`, whose specifiers can depend on
  `lc_time_names`.

## Adding a command

Drop a new module in `cogs/`. It is discovered automatically at startup; no
registration list to update.

```python
# src/discord_bot_v3/cogs/fun.py
import discord
from discord.ext import commands


class Fun(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @discord.slash_command(name="roll", description="Roll a die.")
    async def roll(
        self,
        ctx: discord.ApplicationContext,
        sides: discord.Option(int, description="How many sides", default=6),
    ) -> None:
        import random

        await ctx.respond(f"You rolled a {random.randint(1, sides)}.")


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Fun(bot))
```

## Pycord gotchas

Pycord is a fork of discord.py and most tutorials you find are for discord.py.
Three differences bite hardest:

- `setup()` and `add_cog()` are **synchronous**. discord.py 2.x made both async;
  copying `async def setup` / `await bot.add_cog(...)` here will fail.
- Never add `from __future__ import annotations` to a module defining slash
  commands. Pycord reads the `discord.Option` objects in the signature at
  runtime, and PEP 563 stringifies them — every option silently becomes a
  required string.
- Cog commands use `@discord.slash_command()`, not `@bot.slash_command()`.

## Notes

- On startup Pycord logs `PyNaCl, davey are not installed, voice will NOT be
  supported`. Harmless unless you need voice — for that, `uv add "py-cord[voice]"`.
- Python 3.14 emits a `SyntaxWarning` from `discord/gateway.py` (`'return' in a
  'finally' block`). It is cosmetic, originates inside Pycord, and does not
  affect behaviour.
- The container runs as the non-root user `bot` (uid 999).
- `LOG_LEVEL=DEBUG` logs every HTTP call to Discord, which is very noisy. Use
  `INFO` unless you are actively debugging.

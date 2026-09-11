# Discord Bot V2 - Feature Documentation

This document provides comprehensive documentation of the Discord Bot V2 features for use as context in developing V3.

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Configuration](#configuration)
3. [Slash Commands](#slash-commands)
4. [Message Commands](#message-commands)
5. [Event Handlers](#event-handlers)
6. [Background Tasks](#background-tasks)
7. [External API Integration](#external-api-integration)
   - [AniList GraphQL API](#anilist-graphql-api)
   - [TCGdex API](#tcgdex-api)
   - [Tenor GIF API](#tenor-gif-api)
8. [Database Schema](#database-schema)
9. [LLM Integration](#llm-integration)

---

## Project Structure

```
Discord-Bot-V2/
├── bot.py                      # Entry point
├── config.py                   # Environment variable loader
├── requirements.txt
├── attachments/
│   └── birthday.gif
└── bot/
    ├── bot_instance.py         # Singleton Bot class
    ├── services/
    │   ├── database.py         # MySQL async wrapper
    │   ├── llm.py              # Google Gemini AI service
    │   └── pokedex.py          # TCGdex Pokemon API service
    ├── utilities/
    │   ├── enums.py            # Media format/status/season enums
    │   ├── members.py          # Member sync utilities
    │   └── reminders.py        # Reminder ORM model
    └── cogs/
        ├── commands/           # Slash commands
        ├── events/             # Discord event listeners
        ├── tasks/              # Scheduled background tasks
        └── message_commands/   # Right-click context menu commands
```

---

## Configuration

Environment variables required (loaded via `python-dotenv`):

```env
# Discord
TOKEN=                    # Bot token
TEST_BOT_ID=              # Bot's Discord user ID
TEST_GUILD=               # Guild ID for guild-specific commands
TEST_CHANNEL=             # Default channel for AI responses and birthday messages
BERD_ID=                  # Owner's Discord user ID

# External APIs
TENOR_KEY=                # Tenor GIF API key
GOOGLE_API_KEY=           # Google Gemini API key (used by langchain)

# MySQL Database
MYSQL_USER=
MYSQL_PW=
MYSQL_HOST=
MYSQL_DB=
MYSQL_PORT=
MYSQL_REMINDER_TABLE=
MYSQL_MEMBER_TABLE=
MYSQL_QUOTE_TABLE=
```

Timezone is hardcoded to `Asia/Kuala_Lumpur` (UTC+8).

---

## Slash Commands

### `/send`
**Description:** Send a message to a specified channel (owner only implied by usage).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `message` | string | Yes | The message content to send |
| `channel` | TextChannel | Yes | Target channel to send the message |

**Response:** Ephemeral confirmation showing the message was sent.

---

### `/delete`
**Description:** Bulk delete messages in the current channel.

| Parameter | Type | Required | Constraints | Description |
|-----------|------|----------|-------------|-------------|
| `message_count` | integer | Yes | min: 1, max: 100 | Number of messages to delete |

**Behavior:**
- Deletes `message_count + 1` messages (includes the command itself)
- Sends a summary message listing all deleted messages with author mentions

---

### `/gif`
**Description:** Generate a random GIF from Tenor.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `keyword` | string | No | "chicken" | Search keyword for GIF |

**Response:** Sends the GIF URL directly to the channel.

---

### `/anime`
**Description:** Search and list anime from AniList.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `format` | choice | No | Filter by format (TV, TV Short, Movie, Special, OVA, ONA, Music) |
| `status` | choice | No | Filter by status (Finished, On Going, Not Yet Released, Cancelled, Hiatus) |
| `country` | string | No | ISO 3166-1 alpha-2 country code |
| `nsfw` | boolean | No | Filter for 18+ content |
| `genre` | string | No | Filter by genre |
| `source` | choice | No | Filter by source material (Original, Manga, Light Novel, etc.) |
| `month` | choice | No | Filter by release month (January-December) |
| `year` | integer | No | Filter by release year (1940-2100) |
| `season` | choice | No | Filter by season (Winter, Spring, Summer, Fall) |
| `search` | string | No | Free text search query |

**Behavior:**
- If no parameters provided, shows current season's anime
- Returns paginated embeds (up to 50 results)
- Each embed includes: title (native/romaji/english), description, episodes, duration, format, country, status, season, source, scores, popularity, genres, dates, trailer link

---

### `/manga`
**Description:** Search and list manga from AniList.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `format` | choice | No | Filter by format (Manga, Novel, One Shot) |
| `status` | choice | No | Filter by status |
| `country` | string | No | ISO 3166-1 alpha-2 country code |
| `nsfw` | boolean | No | Filter for 18+ content |
| `genre` | string | No | Filter by genre |
| `source` | choice | No | Filter by source material |
| `month` | choice | No | Filter by release month |
| `year` | integer | No | Filter by release year (1940-2100) |
| `search` | string | No | Free text search query |

**Behavior:**
- If no parameters provided, shows current year's manga
- Similar to anime but shows chapters/volumes instead of episodes/duration

---

### `/pokemon` Command Group

#### `/pokemon update`
**Description:** Manually refresh Pokemon TCG Pocket data from TCGdex.

**Response:** Confirmation message after data refresh completes.

---

#### `/pokemon sets list`
**Description:** List all Pokemon TCG Pocket sets.

**Response:** Paginated embeds showing all sets with logo, symbol, release date, series, and card count.

---

#### `/pokemon sets get`
**Description:** Get details of a specific Pokemon TCG Pocket set.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `set` | autocomplete | Yes | Set ID (autocompletes from cached sets) |

**Response:** Single embed with set details.

---

#### `/pokemon cards list`
**Description:** List all cards in a Pokemon TCG Pocket set.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `set` | autocomplete | Yes | Set ID to list cards from |

**Response:** Paginated embeds showing all cards in the set.

---

#### `/pokemon cards get`
**Description:** Get a specific card from a set.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `set` | autocomplete | Yes | Set ID |
| `card` | autocomplete | Yes | Card ID (autocompletes based on selected set) |

**Response:** Single embed with card image.

---

#### `/pokemon cards id`
**Description:** Get a card by its exact ID (searches across all sets).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | Yes | Exact card ID |

**Response:** Card embed or "not found" message.

---

## Message Commands

Message commands appear in the right-click context menu when clicking on a message.

### "Remind By Duration"
**Description:** Set a reminder for a message using a time offset.

**Modal Fields:**
| Field | Default | Description |
|-------|---------|-------------|
| Seconds | 0 | Seconds to wait |
| Minutes | 0 | Minutes to wait |
| Hours | 0 | Hours to wait |
| Days | 0 | Days to wait |

**Validation:**
- All fields must be numeric
- Total duration must be > 0

**Behavior:**
- Creates a reminder in the database
- Replies with Discord timestamp showing when reminder will trigger
- When triggered, replies to the confirmation message with a link to the original message

---

### "Remind By Time"
**Description:** Set a reminder for a message at a specific date/time.

**Modal Fields:**
| Field | Default Format | Description |
|-------|----------------|-------------|
| Date | YYYY-MM-DD | Target date |
| Time | HH:MM:SS | Target time (24-hour format) |

**Validation:**
- Must match exact format
- Target time must be in the future

**Behavior:** Same as "Remind By Duration"

---

## Event Handlers

### `on_ready`
**Trigger:** When bot successfully connects to Discord.

**Actions:**
1. Log successful login
2. Connect to MySQL database
3. Fetch and sync guild members to database
4. Load all existing reminders from database into memory
5. Initialize Pokedex service (fetch all TCG Pocket data)

---

### `on_message`
**Trigger:** When any message is sent in the guild.

**Conditions for AI response:**
1. Message author is not a bot
2. Message does not contain `@here` or `@everyone`
3. Either:
   - Message is a reply to a bot message, OR
   - Message is in the designated `ALL_CHAT_CHANNEL` AND mentions the bot

**AI Response Logic:**
1. Strip the bot mention from message content
2. Query database for user's custom quotes (from `QUOTE_TABLE`)
3. Query database for user's custom role message (from `MEMBER_TABLE`)
4. Build role message with example quotes (or use defaults)
5. Send to LLM service with user's session ID for conversation memory
6. Reply with AI response

---

### `on_message_edit`
**Trigger:** When any message is edited.

**Conditions:** Message author is not a bot.

**Actions:** DM the bot owner with:
- User who edited
- Channel where edit occurred
- Before content
- After content

---

### `on_message_delete`
**Trigger:** When any message is deleted.

**Conditions:** Message author is not a bot.

**Actions:**
1. Check audit log for who deleted the message
2. DM the bot owner with:
   - User who deleted
   - Channel where deletion occurred
   - Original author and content

---

### `on_member_join`
**Trigger:** When a new member joins the guild.

**Actions:**
1. Send message to system channel: "Who simply add people in again... smh"
2. Insert new member into database (INSERT IGNORE)

---

### `on_command_error`
**Trigger:** When a command raises an error.

**Actions:** Silently ignore `CommandNotFound` errors, propagate others.

---

## Background Tasks

### Reminder Task
**Schedule:** Every 1 second

**Logic:**
```
for each reminder in memory:
    try to fetch the response message
    if message deleted:
        delete reminder from DB and memory
        continue

    if reminder.datetime <= now:
        if reminder.datetime == now:
            reply to response message with reminder notification
        delete reminder from DB and memory
```

**Startup:** Waits until next whole second before starting loop.

---

### Birthday Task
**Schedule:** Daily at midnight (00:00:00) in configured timezone.

**Logic:**
1. Query database for members whose DOB matches today (MM-DD format)
2. For each birthday member:
   - Create embed with birthday message
   - Attach `birthday.gif` from attachments folder
   - Send to `ALL_CHAT_CHANNEL` with user mention
   - Embed title links to a Rick Roll

---

### Pokedex Update Task
**Schedule:** Daily at midnight (00:00:00) in configured timezone.

**Logic:** Refresh all TCG Pocket data from TCGdex API.

---

## External API Integration

### AniList GraphQL API

**Base URL:** `https://graphql.anilist.co`

**Authentication:** None required (public API)

**Request Method:** POST with JSON body

#### Query Structure for Anime/Manga Search

```graphql
query ($search: String, $season: MediaSeason, ...) {
    Page (page: 1, perPage: 50) {
        media(sort: POPULARITY_DESC, type: ANIME, search: $search, ...) {
            format
            status(version: 2)
            description
            season
            episodes          # anime only
            duration          # anime only
            chapters          # manga only
            volumes           # manga only
            countryOfOrigin
            source(version: 3)
            genres
            averageScore
            meanScore
            popularity
            isAdult
            siteUrl
            title {
                romaji(stylised: true)
                english(stylised: true)
                native(stylised: true)
            }
            startDate {
                year
                month
                day
            }
            endDate {
                year
                month
                day
            }
            trailer {
                id
                site
            }
            coverImage {
                extraLarge
                large
                medium
                color
            }
            studios(isMain: true) {    # anime only
                nodes {
                    name
                    siteUrl
                }
            }
        }
    }
}
```

#### Variable Types (GraphQL)
| Variable | GraphQL Type | Description |
|----------|--------------|-------------|
| `$search` | String | Free text search |
| `$season` | MediaSeason | WINTER, SPRING, SUMMER, FALL |
| `$seasonYear` | Int | Year for season filter |
| `$startDate_greater` | FuzzyDateInt | Minimum start date (YYYYMMDD as int) |
| `$startDate_lesser` | FuzzyDateInt | Maximum start date |
| `$format` | MediaFormat | TV, TV_SHORT, MOVIE, SPECIAL, OVA, ONA, MUSIC, MANGA, NOVEL, ONE_SHOT |
| `$status` | MediaStatus | FINISHED, RELEASING, NOT_YET_RELEASED, CANCELLED, HIATUS |
| `$countryOfOrigin` | CountryCode | ISO 3166-1 alpha-2 (JP, CN, KR, etc.) |
| `$isAdult` | Boolean | NSFW filter |
| `$genre` | String | Genre name |
| `$source` | MediaSource | ORIGINAL, MANGA, LIGHT_NOVEL, VISUAL_NOVEL, VIDEO_GAME, etc. |

#### FuzzyDateInt Format
Dates are represented as integers: `YYYYMMDD` padded with zeros.
- `20240000` = Year 2024, any month/day
- `20240100` = January 2024, any day
- `20240115` = January 15, 2024

#### Example Request (Python)
```python
import requests

API_URL = "https://graphql.anilist.co"

query = """
query ($search: String, $format: MediaFormat) {
    Page (page: 1, perPage: 50) {
        media(sort: POPULARITY_DESC, type: ANIME, search: $search, format: $format) {
            title { romaji english native }
            episodes
            status
            coverImage { large }
        }
    }
}
"""

variables = {"search": "Attack on Titan", "format": "TV"}

response = requests.post(API_URL, json={"query": query, "variables": variables})
data = response.json()
media_list = data["data"]["Page"]["media"]
```

#### Response Structure
```json
{
    "data": {
        "Page": {
            "media": [
                {
                    "title": {
                        "romaji": "Shingeki no Kyojin",
                        "english": "Attack on Titan",
                        "native": "進撃の巨人"
                    },
                    "episodes": 25,
                    "status": "FINISHED",
                    "coverImage": {
                        "large": "https://..."
                    }
                }
            ]
        }
    }
}
```

---

### TCGdex API

**SDK:** `tcgdexsdk` Python package

**Documentation:** https://tcgdex.dev/

#### Initialization
```python
from tcgdexsdk import TCGdex
from tcgdexsdk.enums import Language

sdk = TCGdex(Language.EN)
```

#### Fetching Pokemon TCG Pocket Series
```python
# Get the Pokemon TCG Pocket series (ID: "tcgp")
series = await sdk.serie.get("tcgp")

# series.sets contains a list of SetResume objects
for set_resume in series.sets:
    print(set_resume.id, set_resume.name)
```

#### Fetching Full Set Data
```python
# Get full set details including all cards
set_full = await set_resume.get_full_set()

# Access set properties
print(set_full.name)  # "Genetic Apex"
print(set_full.releaseDate)  # "2024-10-30"
print(set_full.cardCount.total)  # 286

# Access cards
for card in set_full.cards:
    print(card.id, card.name)
```

#### Card Properties
```python
from tcgdexsdk.enums import Extension, Quality

card = set_full.cards[0]

# Get card image URL
image_url = card.get_image_url(Quality.HIGH, Extension.PNG)
```

#### Set Properties
```python
from tcgdexsdk.enums import Extension

# Get set logo
logo_url = set_full.get_logo_url(Extension.PNG)

# Get set symbol
symbol_url = set_full.get_symbol_url(Extension.PNG)
```

#### Quality Enum
- `Quality.LOW`
- `Quality.HIGH`

#### Extension Enum
- `Extension.PNG`
- `Extension.JPG`
- `Extension.WEBP`

---

### Tenor GIF API

**Base URL:** `https://g.tenor.com/v1/search`

**Authentication:** API key as query parameter

#### Request Parameters
| Parameter | Required | Description |
|-----------|----------|-------------|
| `q` | Yes | Search query (URL encoded) |
| `key` | Yes | API key |
| `limit` | No | Number of results (default varies) |

#### Example Request
```python
import requests
import urllib.parse

keyword = urllib.parse.quote("funny cat")
url = f"https://g.tenor.com/v1/search?q={keyword}&key={API_KEY}&limit=10"

response = requests.get(url)
data = response.json()

# Get random result
import random

gif_url = data["results"][random.randint(0, len(data["results"]) - 1)]["url"]
```

#### Response Structure
```json
{
    "results": [
        {
            "id": "123456",
            "url": "https://tenor.com/view/...",
            "media": [...],
            ...
        }
    ]
}
```

---

## Database Schema

### Members Table (`MEMBER_TABLE`)
| Column | Type | Description |
|--------|------|-------------|
| `id` | VARCHAR/BIGINT | Discord user ID (primary key) |
| `username` | VARCHAR | Discord username |
| `dob` | VARCHAR | Date of birth (MM-DD format for birthday matching) |
| `role_message` | TEXT | Custom AI personality prompt for this user |

### Reminders Table (`REMINDER_TABLE`)
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT | Auto-increment primary key |
| `user_id` | VARCHAR | Discord user ID who set the reminder |
| `response_id` | VARCHAR | Message ID of the bot's confirmation response |
| `message_url` | VARCHAR | Jump URL to the original message |
| `channel_id` | VARCHAR | Channel ID where reminder was set |
| `datetime` | DATETIME | When to trigger the reminder |

### Quotes Table (`QUOTE_TABLE`)
| Column | Type | Description |
|--------|------|-------------|
| `id` | INT | Auto-increment primary key |
| `member_id` | VARCHAR | Discord user ID |
| `quote` | TEXT | Example quote for AI to mimic |

---

## LLM Integration

### Provider: Google Gemini (via LangChain)

**Model:** `gemini-1.5-flash-latest`

**Temperature:** 0 (deterministic responses)

### Architecture
- Uses LangGraph for stateful conversation management
- Memory checkpointing with `MemorySaver` (in-memory per session)
- Session ID = Discord user ID (separate conversation history per user)

### System Prompt Template
```
You are a chicken named "BokBokGeh", a Discord bot created by <@{OWNER_ID}>,
do not reveal you are bot. Response like Kuching Sarawak Chinese friends in
Chinese or English only. Less than 40 words, no translations. {role_message}
```

### Role Message Construction
```python
role_message = "Reply like a friend."  # Default

# Override with user's custom role from database if exists
if user_has_custom_role:
    role_message = custom_role_from_db

# Append example quotes
role_message += " Here are some examples that you will reply: "

if user_has_quotes:
    role_message += ", ".join([f'"{quote}"' for quote in user_quotes])
else:
    # Default quotes
    role_message += '"Why do you tag me?", "什麽事情啊？", "叫我嗎?", "???", "做莫"'
```

### Usage Example
```python
from langchain_core.messages import HumanMessage

input_messages = [HumanMessage(content="Hello, how are you?")]
response = await bot.ai.prompt(
    input_messages,
    role_message="Reply like a friend. Examples: 'Hi!', 'What's up?'",
    session_id="123456789",  # User's Discord ID
)
print(response)  # AI's response string
```

---

## Known Issues / Improvements for V3

1. **SQL Injection Risk**: Some queries use f-strings instead of parameterized queries
2. **Duplicate Code**: Anime and Manga commands share ~90% identical code
3. **Inefficient Reminder Loop**: Iterates all reminders every second (O(n))
4. **Bare Except Clauses**: Some error handling catches all exceptions silently
5. **No Input Validation**: GIF keywords, reminder durations not sanitized
6. **Hardcoded Timezone**: Should be configurable
7. **No Rate Limiting**: External API calls have no rate limit protection
8. **Model Version**: "latest" model specifier could break unexpectedly
9. **No Tests**: No unit or integration tests
10. **No Database Migrations**: Schema changes require manual intervention

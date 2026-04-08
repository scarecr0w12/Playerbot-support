# 🤖 Discord Support & Moderation Bot

A full-featured, modular Discord bot with **19 cogs** for **AI-powered support**, **ticketing**, **moderation**, **economy**, **leveling**, **giveaways**, **reminders**, **starboard**, **keyword highlights**, **custom commands**, **reporting**, **GitHub integration**, and **server management** — backed by any OpenAI-compatible LLM and **Qdrant** vector database for scalable RAG.

Inspired by [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot), Ticket Tool, Carl-bot, and MEE6.

---

## ✨ Features (19 Cogs)

### 💬 AI Assistant (LLM-Powered) — *Inspired by [VRT-Cogs/assistant](https://github.com/vertyco/vrt-cogs)*

- **/chat** `<question>` — Talk to the AI with **per-user, per-channel** conversation memory.
- **/ask** — Alias for /chat. **Right-click context menu** → "Ask AI" on any message.
- **/draw** `<prompt>` — **DALL-E image generation** (size, quality, style options).
- **/tldr** — **Summarise recent channel messages** with optional focused question.
- **/convostats** / **/convoclear** / **/convopop** — Conversation stats, clear, pop last message.
- **/compact** — **LLM-powered conversation compaction** (summarises history to save tokens).
- **/convoprompt** — Set a per-channel system prompt override.
- Works with **OpenAI, Ollama, LM Studio, vLLM**, or any OpenAI-compatible API.

#### RAG Knowledge Base (Embeddings + Qdrant)

- **/embeddings add/update/remove/list/reset** — Manage knowledge entries with auto-vectorisation.
- **/embeddings crawl** `<url>` / **crawl_site** `<url>` — Crawl a page or whole site, chunk, embed, and store in the knowledge base.
- **/query** — Test embedding similarity search against the knowledge base.
- Vectors stored in **[Qdrant](https://qdrant.tech/)** per-guild collections (`embeddings_{guild_id}`); metadata in SQLite.
- Relevant knowledge automatically injected into conversations via cosine similarity.
- Configurable **minimum relatedness threshold**.
- **Adaptive learning** — key facts extracted from exchanges and stored in `facts_{guild_id}`; injected only when highly relevant (threshold 0.55).

#### Function Calling / Custom Tools

- Built-in tools: **get_time**, **create_embed** (renders Discord embeds via AI).
- **/customfunctions** — Define custom functions with JSON schema + Python code.
- **/listfunctions** / **/togglefunctions** — Manage & enable/disable functions per guild.

#### Auto-Response & Triggers

- **Listen channels** — Bot auto-responds to every message in designated channels.
- **@mention responses** — Bot responds when mentioned.
- **Regex trigger phrases** — Bot responds when messages match configured patterns.

#### Feedback & Learning

- Responses include **👍 / 👎 buttons** — ratings logged to the database and dashboard.
- Works on both `/chat` slash command replies and direct message/mention responses.
- **/assistant learningstats** — view learned facts count, total ratings, positive %, satisfaction.
- **/assistant negativefeedback** — show recent poorly-rated Q&A pairs.
- **/assistant togglelearning** / **resetfeedback** — control adaptive learning and clear data.

#### Admin Configuration (`/assistant …`)

- **toggle** · **model** · **temperature** · **maxtokens** · **maxretention**
- **prompt** (with `{placeholders}`) · **channelprompt** · **functioncalls** · **toggledraw**
- **relatedness** · **listen** · **mention** · **trigger** · **triggerlist**
- **usage** · **resetusage** · **resetconversations** · **view**
- **Token usage tracking** per guild with stats and reset.
- **/help_support** — Full command reference.

### 🎫 Ticket System
- **Button-based ticket creation** — Admin posts a panel; users click to open.
- **Modal popup** collects subject & description before creating the channel.
- **Private channel per ticket** with permission-controlled access.
- **Claim / Transcript / Close** buttons inside every ticket.
- **/ticket_panel**, **/ticket_category**, **/ticket_close**

### 🛡️ Full Moderation
- **/warn**, **/mute**, **/unmute**, **/kick**, **/ban**, **/unban**
- **Case tracking** — every action recorded with case ID, reason, moderator, timestamp.
- **Warning accumulation** — configurable threshold triggers automatic mute/kick/ban.
- **Reason modal** support for UI-driven moderation.
- **/warnings**, **/clearwarnings**, **/modlog**

### 📋 Mod Logging
- Embed-based audit logs sent to a configurable mod-log channel.
- Logs: mod actions, message edits/deletes, ticket events, auto-mod triggers, reports.
- **/setmodlog**

### 🤖 Auto-Moderation
- **Spam detection** — rate-limit messages per user.
- **Word filter** — block messages with banned words/phrases.
- **Link filter** — block messages with banned domains.
- Staff exempt. **/filter add_word/remove_word/add_link/remove_link/list**, **/automod_toggle**

### 👋 Welcome System
- **Configurable welcome messages** with `{user}`, `{username}`, `{server}` placeholders.
- **Auto-role** on join.
- **Rules acceptance panel** with persistent button → grants "Verified" role.
- **/set_welcome_channel**, **/set_welcome_message**, **/set_autorole**, **/set_verified_role**, **/rules_panel**

### � Admin
- **Role management** — **/role add**, **/role remove**, **/role members**
- **Self-assignable roles** — **/selfrole add/remove/list/panel** with select-menu UI
- **Nickname management** — **/nick**
- **Announcements** — **/announce** (rich embed to any channel)
- **Server config viewer** — **/serverconfig**

### 🗑️ Cleanup
- **/purge** `<count>` — Delete N messages.
- **/purge_user** `<member>` — Delete messages from a specific user.
- **/purge_bots** — Delete bot messages.
- **/purge_contains** `<text>` — Delete messages containing text.
- **/purge_embeds** — Delete messages with embeds/attachments.

### ✏️ Custom Commands
- **/cc add** `<name>` `<response>` — Create text-response commands.
- **/cc edit**, **/cc delete**, **/cc list**, **/cc info**
- Supports **variables**: `{user}`, `{username}`, `{server}`, `{channel}`, `{members}`
- Triggered with `!commandname` prefix.

### 💰 Economy / Bank
- **/balance** — Check your (or another user's) balance.
- **/payday** — Collect daily credits (configurable amount & cooldown).
- **/transfer** — Send credits to another user.
- **/slots** `<bet>` — Slot machine gambling with jackpots.
- **/leaderboard** — Richest members.
- **/econset** — Admin commands for payday amount, cooldown, currency name, set balance.

### 📩 Reports
- **/report** `<user>` `<reason>` — Report a user to staff.
- **Right-click context menu** → "Report User" with a modal popup.
- **/reports** — Staff view of open reports.
- **/report_resolve** — Resolve or dismiss a report.
- **/set_reports_channel** — Configure where report notifications go.

### 🧰 Utility / General

- **/userinfo**, **/serverinfo**, **/avatar**, **/botinfo**, **/ping**
- **/poll** `<question>` — Reaction-based yes/no/maybe poll.
- **/8ball**, **/coinflip**, **/roll**, **/choose**

### 🔐 Permissions
- Fine-grained per-command overrides beyond Discord's built-in system.
- **/perm allow_role/deny_role**, **/perm allow_channel/deny_channel**
- **/perm allow_user/deny_user**, **/perm reset**, **/perm show**
- Enforced via a global interaction check — denials are silent and ephemeral.

### ⭐ XP / Leveling — *Inspired by MEE6 & Red-DiscordBot LevelUp*

- Members earn **15–25 XP per message** with a configurable cooldown (default 60 s).
- Level-up formula: `5×L² + 50×L + 100` XP needed per level.
- **Level-up announcements** sent to a configurable channel (or the message channel).
- **Level roles** — automatically assign/remove roles when members hit thresholds.
- **/rank** — embed showing level, XP, rank position, and progress bar.
- **/levels leaderboard** — top XP earners.
- **/levels toggle / set_announce / set_cooldown / set_xp_rate** — admin config.
- **/levels add_role / remove_role / exclude_channel / set_xp / reset** — admin tools.

### 🎉 Giveaways — *Inspired by GiveawayBot & Carl-bot*

- **/giveaway start** `<prize> <duration> [winners] [channel]` — creates a live giveaway embed.
- Entry via a **🎉 Enter button** (click again to withdraw your entry).
- Duration format: `1d`, `2h`, `30m`, `1d12h30m`, etc.
- **Automatic winner selection** when the timer expires (background task, every 30 s).
- Winners are DM'd and mentioned in the giveaway channel.
- **/giveaway end / reroll / cancel / list / info** — full management suite.
- Giveaway views are **persistent** across bot restarts.

### ⏰ Reminders — *Inspired by Red-DiscordBot Reminder cog*

- **/remindme** `<time> <message>` — set a reminder via relative (`1h30m`, `2d`) or absolute (`2026-04-10 14:30`) time.
- Bot **DMs you** (or pings in channel) when the reminder fires.
- **/reminders list** — view all your pending reminders with timestamps.
- **/reminders delete `<id>`** — cancel a specific reminder.
- **/reminders clear** — cancel all your reminders at once.
- Background task fires due reminders every 30 s, persists across restarts.

### ⭐ Starboard — *Democratic message pinning*
- Members react with ⭐ (configurable) to vote messages onto the starboard channel.
- **Configurable threshold** (default 3 stars); self-reactions and bots don't count.
- Starboard embed **auto-updates** star count; post is **removed** if stars drop below threshold.
- Handles message deletions, image attachments, and rich embeds.
- **/starboard set_channel / set_threshold / set_emoji / toggle / ignore_channel / info**

### 🔔 Highlights / Keywords — *Inspired by Discord's built-in highlights*
- **/highlight add `<keyword>`** — subscribe to a word/phrase; get a **DM with context** whenever it's mentioned.
- Full-word, case-insensitive matching with a per-user, per-keyword 60 s DM cooldown.
- Includes **3 lines of message context** in each notification.
- Respects channel read permissions — no notifications from channels you can't see.
- **/highlight remove / list / clear / pause** — full self-service management.
- Up to **25 keywords per user per guild**.

### 🐙 GitHub Integration

#### Repo Monitoring (polling every 60 s)
- Automatically posts rich embeds to subscribed channels for: **pushes**, **pull requests**, **issues**, **releases**.
- Per-guild, per-channel subscriptions with configurable event filters.
- Uses HTTP ETags for efficient conditional requests (avoids burning rate-limit quota).

#### GitHub API Commands (`/github …`)
- **/github repo** `<owner/repo>` — rich repo overview (stars, forks, language, topics, license).
- **/github user** `<username>` — GitHub user profile (bio, repos, followers, location).
- **/github issue** `<owner/repo> <number>` — look up any issue or PR by number.
- **/github issues** `<owner/repo>` — list open issues (optional label filter).
- **/github prs** `<owner/repo>` — list open pull requests.
- **/github releases** `<owner/repo>` — latest releases with assets and pre-release flags.
- **/github search** `<query>` — search GitHub repositories (sort by stars/forks/updated).
- **/github ratelimit** — view current API rate-limit status.

#### Subscription Management
- **/github subscribe** `<repo> [channel] [events]` — subscribe a channel to a repo's events.
- **/github unsubscribe** `<repo> [channel]` — remove a subscription.
- **/github subscriptions** — list all active subscriptions for this server.

#### RAG Ingestion

- **/github ingest** `<owner/repo> [branch]` — fetch the repo's README and `docs/` files and ingest them into the AI knowledge base for RAG-assisted answers.

---

## 🚀 Quick Start

### 1. Prerequisites

- **Python 3.10+**
- A **Discord bot token** — [create one here](https://discord.com/developers/applications)
- An LLM endpoint (OpenAI API key, running Ollama instance, etc.)
- **[Qdrant](https://qdrant.tech/)** vector database (self-hosted or cloud)

  ```bash
  docker run -d --name qdrant -p 6333:6333 qdrant/qdrant
  ```

### 2. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env — see table below
```

| Variable | Description | Default |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Discord bot token | *(required)* |
| `LLM_BASE_URL` | OpenAI-compatible API base URL | `https://api.openai.com/v1` |
| `LLM_API_KEY` | API key (`no-key-needed` for local) | `no-key-needed` |
| `LLM_MODEL` | Model name | `gpt-3.5-turbo` |
| `SYSTEM_PROMPT` | System prompt for the AI | *see .env.example* |
| `MAX_HISTORY_TURNS` | Conversation turns per user | `20` |
| `DEFAULT_MUTE_DURATION_MINUTES` | Default mute length | `10` |
| `MAX_WARNINGS_BEFORE_ACTION` | Warns before auto-action | `3` |
| `WARNING_ACTION` | Auto-action: `mute` / `kick` / `ban` | `mute` |
| `AUTOMOD_SPAM_THRESHOLD` | Messages to trigger spam | `5` |
| `AUTOMOD_SPAM_INTERVAL` | Spam window in seconds | `5` |
| `GITHUB_TOKEN` | GitHub Personal Access Token (optional) | *(empty)* |
| `QDRANT_URL` | Qdrant server URL | `http://localhost:6333` |
| `QDRANT_API_KEY` | Qdrant API key (cloud only) | *(empty)* |

Economy settings (payday amount, cooldown, currency name) are configurable per-guild via `/econset` commands.

#### Example: OpenAI
```env
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-your-key
LLM_MODEL=gpt-4o
```

#### Example: Ollama (local)
```env
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=no-key-needed
LLM_MODEL=llama3
```

### 4. Discord Developer Portal Setup

1. Create an application at [discord.com/developers](https://discord.com/developers/applications).
2. **Bot** tab → enable **Message Content Intent** and **Server Members Intent**.
3. Copy the bot token into `.env`.
4. **OAuth2 → URL Generator** → scopes: `bot`, `applications.commands`.
5. Permissions: `Administrator` (or fine-grained: Send Messages, Manage Channels, Manage Roles, Kick/Ban Members, Moderate Members, Embed Links, Attach Files, Read Message History, Use Slash Commands).
6. Invite the bot with the generated URL.

### 5. Run

```bash
python main.py
```

Slash commands sync on startup (may take ~1 minute to appear in Discord).

### 6. Migrate Existing Knowledge Base (first run only)

If you have existing embedding rows in SQLite from a previous version, run once to re-embed and push them into Qdrant:

```bash
python migrate_to_qdrant.py
```

Rows that already have a `qdrant_id` are skipped automatically.

### 7. First-Time Server Setup

Once the bot is online, run these in your server:

```
/setmodlog channel:#mod-log
/ticket_category category:Support
/ticket_panel channel:#support
/set_welcome_channel channel:#welcome
/set_welcome_message message:Welcome to **{server}**, {user}! 🎉
/set_autorole role:@Member
/set_verified_role role:@Verified
/rules_panel channel:#rules rules_text:Be respectful. No spam.
/automod_toggle enabled:True
/set_reports_channel channel:#staff-reports
/selfrole add role:@Gamer
/selfrole panel channel:#roles
/econset payday_amount amount:150
```

---

## 📁 Project Structure

```
├── main.py                         # Entry point — wires all services & cogs
├── migrate_to_qdrant.py            # One-time migration: SQLite blobs → Qdrant
├── bot/
│   ├── config.py                   # Environment-based configuration
│   ├── database.py                 # Shared async SQLite DB (all tables)
│   ├── llm_service.py              # LLM client (chat, embeddings, image gen, compaction)
│   ├── qdrant_service.py           # Qdrant vector DB wrapper (per-guild collections)
│   └── cogs/
│       ├── support.py              # Full AI assistant (chat, RAG, functions, draw, tldr)
│       ├── highlights.py           # Keyword notifications (DMs on keyword match)
│       ├── github.py               # GitHub integration (monitoring, API, RAG ingest)
│       ├── tickets.py              # Ticket system (modals, buttons, channels)
│       ├── moderation.py           # warn/mute/kick/ban with case tracking
│       ├── mod_logging.py          # Embed audit logs to mod-log channel
│       ├── automod.py              # Spam, word filter, link filter
│       ├── welcome.py              # Welcome messages, autorole, rules panel
│       ├── admin.py                # Role mgmt, selfrole, nick, announce
│       ├── cleanup.py              # Bulk message deletion / purge
│       ├── custom_commands.py      # User-defined text commands
│       ├── economy.py              # Bank, payday, slots, leaderboard
│       ├── reports.py              # User → staff reporting system
│       ├── utility.py              # userinfo, serverinfo, poll, 8ball, etc.
│       └── permissions.py          # Per-command permission overrides
├── dashboard/                      # FastAPI web dashboard
│   ├── app.py                      # Routes, auth, crawl API
│   ├── templates/                  # Jinja2 HTML templates
│   └── static/                     # CSS / JS assets
├── data/                           # Auto-created — stores bot.db
├── requirements.txt
├── .env.example
└── README.md
```

### Architecture

The bot follows a **cog-based modular architecture** (inspired by Red-DiscordBot):

- **19 independent cogs** — each encapsulates a complete feature.
- All cogs share a single **async SQLite database** (`bot/database.py`) with 18+ tables.
- **Qdrant** handles all vector storage: `embeddings_{guild_id}` (knowledge base) and `facts_{guild_id}` (learned facts). SQLite stores only metadata and `qdrant_id` references.
- **ModLogging** and **Permissions** load first; other cogs reference them.
- **Persistent views** (ticket panels, rules acceptance, self-role menus) survive bot restarts.
- **Global interaction check** enforces custom permission overrides from the Permissions cog.
- The LLM service and QdrantService are injected only into the Support cog.
- Custom commands trigger via `!` prefix; all other commands use Discord slash commands.
- The **FastAPI dashboard** runs alongside the bot in the same process via `uvicorn`.

### Database Tables

| Table | Used by |
|---|---|
| `guild_config` | All cogs (key-value per-guild settings) |
| `mod_cases` | Moderation, ModLogging |
| `warnings` | Moderation |
| `tickets` | Tickets |
| `ticket_messages` | Tickets (transcript) |
| `automod_filters` | AutoMod |
| `conversation_history` | Support (per-user, per-channel LLM conversations) |
| `embeddings` | Support (RAG knowledge base metadata; vectors in Qdrant) |
| `custom_functions` | Support (custom function calling definitions) |
| `token_usage` | Support (per-guild token usage tracking) |
| `assistant_triggers` | Support (regex trigger phrases) |
| `learned_facts` | Support (adaptive learning — facts extracted from exchanges) |
| `response_feedback` | Support (👍/👎 ratings on bot responses) |
| `economy_accounts` | Economy |
| `custom_commands` | CustomCommands |
| `reports` | Reports |
| `selfroles` | Admin |
| `command_permissions` | Permissions |

---

## License

MIT

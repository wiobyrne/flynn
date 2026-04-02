# Flynn

A personal AI assistant that connects Telegram to your Obsidian vault. Send it anything — it routes tasks to the right area of your life using local AI (Ollama), no cloud required.

Named after Flynn from Tron.

## What it does

- **Quick capture** — send any message and Flynn routes it to the right domain in your vault and your daily note
- **Intent detection** — reflections ("I feel...") go to your daily note's Notes section; tasks go to the inbox
- **Date-aware capture** — "tomorrow I need to..." schedules to the right date automatically
- **Daily briefing** — auto-pushed every morning with domain status and next actions
- **Structured check-ins** — morning and evening prompts with mood/sleep/energy tracking into frontmatter
- **Task management** — `/done` shows a numbered list to pick from; `/list`, `/focus` to manage from your phone
- **Local AI routing** — uses Ollama first, falls back to Claude API, then keywords. Your data never leaves your network.
- **Always on** — runs as a systemd service, survives reboots

## How it works

```
Telegram (your phone)
    ↓
Flynn bot (Python, systemd)
    ↓ detects intent (task vs reflection)
    ↓ parses date reference (today/tomorrow/day name)
    ↓ classifies via
Ollama (local) → Claude API (fallback) → keywords (fallback)
    ↓ writes to
Bot Inbox.md (tagged tasks) + daily note (Tasks Quick Add or Notes)
    ↓ surfaced by
Domain notes (Tasks plugin) + Flynn.base (Obsidian Bases dashboard)
```

## Setup

### 1. Prerequisites

- Python 3.11+
- [Ollama](https://ollama.ai) with at least one model (`ollama pull llama3.1:8b`)
- An Obsidian vault
- A Telegram account

### 2. Install

```bash
git clone https://github.com/wiobyrne/flynn
cd flynn
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3. Create your Telegram bot

1. Message `@BotFather` on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the token BotFather gives you
4. Send a message to your new bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Find `"chat": {"id": ...}` — that number is your chat ID

### 4. Configure

```bash
cp .env.example .env
# Edit .env with your Telegram token and chat ID

# Edit config.yaml:
# - Set vault_path to your Obsidian vault
# - Adjust domains to match your life areas
# - Set ollama.model to a model from `ollama list`
```

### 5. Set up your Obsidian vault

Create a `00 DOMAINS/` folder with one note per domain:

```yaml
---
title: Self
domain: self
focus: active
energy: medium
next_action: ""
last_updated: 2026-03-31
---
```

Add a Tasks plugin query to each domain note:

````markdown
```tasks
not done
tags include #domain/self
limit 5
```
````

Create `01 CONSUME/📥 Inbox/Bot Inbox.md` — Flynn appends captured tasks here.

Create daily notes at `03 CREATE/Journal/Daily/YYYY/MM/YYYY-MM-DD.md` — Flynn writes tasks and reflections here too. You can adjust the path in `config.yaml`.

### 6. Run

```bash
venv/bin/python assistant.py
```

### 7. Run as a service (Linux)

Create a wrapper script if your path contains spaces:

```bash
# ~/flynn-start.sh
#!/bin/bash
exec "/path/to/flynn/venv/bin/python" "/path/to/flynn/assistant.py"
```

Create the service:

```bash
sudo nano /etc/systemd/system/flynn.service
```

```ini
[Unit]
Description=Flynn AI Assistant Bot
After=network.target

[Service]
ExecStart=/home/youruser/flynn-start.sh
WorkingDirectory=/home/youruser
Restart=on-failure
User=youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable flynn
sudo systemctl start flynn
```

## Commands

| Command | Description |
|---------|-------------|
| `/today` | Morning briefing — domain status, next actions, overdue flags |
| `/status` | Bar chart of open tasks per domain |
| `/list [domain]` | Show open tasks, optionally filtered by domain |
| `/done` | Pick a task to mark complete from a numbered list |
| `/done <text>` | Mark a matching task complete by partial text |
| `/focus <domain> <text>` | Set next action on a domain note |
| `/week` | Weekly digest — stats per domain + creates weekly note in vault |
| `/add <text>` | Explicit task capture (bypasses intent detection) |
| `/journal <text>` | Save a note directly to today's daily note |
| Any text | Auto-routed — task or reflection detected automatically |

## How capture works

**Tasks** — anything with "I need to", "remind me", "schedule", etc. goes to:
- `Bot Inbox.md` tagged `#domain/X 📅 YYYY-MM-DD`
- Today's (or tomorrow's) daily note under Tasks Quick Add

**Reflections** — "I feel", "today was", "I noticed", "grateful for", etc. go to:
- Today's daily note under Notes

**Date references** — "tomorrow", "monday", "this week" are detected and used to schedule the task to the right date.

## Check-ins

Flynn sends structured check-ins at configurable times:

**Morning (default 7:05am):**
1. Sleep (1–5)
2. Mood (1–5)
3. Anxiety — anything weighing on you?
4. Grateful — one thing
5. Intention — what would make today good?

**Evening (default 5:30pm):**
1. Energy (1–5)
2. Wins — what went well?
3. Friction — what was hard?
4. Tomorrow — one thing to carry forward

Numeric scores are parsed and written into the daily note's YAML frontmatter (`sleep`, `mood`, `energy`) so you can query them across your vault over time.

A daily briefing (domain summary) auto-pushes at 7:00am before the morning check-in.

## Configuration

```yaml
vault_path: "~/Documents/your-vault"
default_domain: "self"
timezone: "America/New_York"

briefing:
  time: "07:00"
  enabled: true

checkins:
  morning:
    time: "07:05"
    enabled: true
  evening:
    time: "17:30"
    enabled: true

ollama:
  url: "http://localhost:11434"
  model: "llama3.1:8b"

domains:
  - id: self
    label: "Self"
    emoji: "🏃"
    description: "health, mental wellness, physical fitness..."
    keywords: [health, workout, sleep, ...]
```

Flynn uses the domain `description` for AI routing and `keywords` as fallback. The more specific your descriptions, the better the routing.

## Cost

Local Ollama routing = $0. Claude API fallback uses `claude-haiku` (cheapest model) only when Ollama is unavailable. For simple task routing, API costs are negligible.

## Obsidian plugins used

- [Tasks](https://obsidian.md/plugins?id=obsidian-tasks-plugin) — for task queries in domain notes
- [Bases](https://obsidian.md/bases) — for the Flynn dashboard (core plugin, Obsidian 1.8+)
- [Dataview](https://obsidian.md/plugins?id=dataview) — optional, for status callouts in domain notes

## FLYNN.md — persistent identity

Create `04 META/🤖 Agents/assistant/FLYNN.md` in your vault to give Flynn persistent context. Flynn reads this file on startup and uses the `## Current Focus` section in every briefing. Edit it like any other note — no code changes needed.

```markdown
## Current Focus
- Launching the new site this week
- Getting class grades submitted

## Notes for Flynn
- "Brighid" routes to family domain
- "initiated" routes to build domain
```

## Weekly notes

Flynn creates weekly notes at `03 CREATE/Journal/Weekly/YYYY-WXX.md` with a domain summary table, overdue task list, and reflection prompts. These are created automatically every Friday at 5pm, or on demand with `/week`.

## Changelog

### v0.4
- FLYNN.md identity file — Flynn reads persistent context from your vault
- Overdue task detection — tasks older than 7 days flagged in every briefing
- Weekly digest — domain stats, overdue tasks, weekly note created in vault
- `/week` command for on-demand weekly review
- Friday auto-digest at configurable time (default 5pm)
- Briefing logic consolidated (shared between /today and auto-push)

### v0.3
- Intent detection: reflections ("I feel...") route to daily note Notes, not task inbox
- Date-aware capture: "tomorrow", day names, "this week" schedule tasks to the right date
- Tasks now write to both Bot Inbox and the target daily note
- Auto-push daily briefing (configurable time, default 7:00am)
- Structured mental health check-ins with mood/sleep/energy score parsing into frontmatter
- `/journal` command for explicit daily note saves
- `/done` with no args shows a numbered list to pick from
- Improved keyword routing (workout, anxiety, grades, etc.)
- Fixed duplicate check-in write bug
- Removed unused imports and dead code

### v0.2
- Daily check-in system (morning and evening prompts)
- Check-in responses saved to daily notes
- Scheduled check-ins via APScheduler

### v0.1
- Initial release
- Telegram → Obsidian task capture
- Ollama routing with Claude API and keyword fallbacks
- `/today`, `/status`, `/list`, `/done`, `/focus` commands
- systemd service setup

## License

MIT

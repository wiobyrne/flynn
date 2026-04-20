# Flynn

A personal AI chief-of-staff that connects Telegram to your Obsidian vault. Send it anything — it routes tasks to the right area of your life using local AI (Ollama), no cloud required.

Named after Flynn from Tron.

## What it does

- **Quick capture** — send any message and Flynn routes it to the right domain in your vault
- **Intent detection** — reflections go to your daily note's Notes section; tasks go to Tasks Quick Add
- **Date-aware capture** — "tomorrow I need to..." schedules to the right date automatically
- **Fleeting notes** — `/note` captures text, voice (Whisper transcription), images, and links to your inbox
- **Structured check-ins** — morning and evening prompts with mood/sleep/energy tracking into frontmatter
- **Mission alignment** — every check-in asks how today connects to your top-level goal
- **Task management** — `/done`, `/list`, `/focus` to manage everything from your phone
- **Local agent API** — other services on your network can send events to Flynn via HTTP
- **Local AI routing** — Ollama first, Claude API fallback, then keywords. Your data never leaves your network.

## How it works

```
Telegram (your phone)          Local agents (homelab, scrapers, etc.)
    ↓                                      ↓
assistant.py               HTTP API http://127.0.0.1:8765
    ↓ detects intent (task vs reflection)
    ↓ parses date reference (today/tomorrow/day name)
    ↓ classifies via
Ollama (local) → Claude API (fallback) → keywords (fallback)
    ↓ writes to
daily note (Tasks Quick Add or Notes) + 01 CONSUME/📥 Inbox/ (fleeting)
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
# Edit .env with your values

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

Create `01 CONSUME/📥 Inbox/` — Flynn saves fleeting notes here.

Create daily notes at `03 CREATE/Journal/Daily/YYYY/MM/YYYY-MM-DD.md` — Flynn writes tasks and reflections here. Adjust the path in `config.yaml` if needed.

### 6. Run

```bash
venv/bin/python assistant.py
```

To keep it running in the background:

```bash
nohup venv/bin/python assistant.py > flynn.log 2>&1 &
```

## Commands

| Command | Description |
|---------|-------------|
| `/today` | Morning briefing — domain status, next actions, overdue flags |
| `/status` | Bar chart of open tasks per domain |
| `/list [domain]` | Show open tasks, optionally filtered by domain |
| `/done` | Pick a task to mark complete from a numbered list |
| `/focus <domain> <text>` | Set next action on a domain note |
| `/week` | Weekly digest — stats per domain + creates weekly note |
| `/add <text>` | Explicit task capture |
| `/journal <text>` | Save a note directly to today's daily note |
| `/note` | Start a fleeting note session (text, voice, image, or link) |
| `/cancel` | Cancel the current capture session |
| Any text | Auto-routed — task or reflection detected automatically |

## Fleeting notes

`/note` turns Flynn into a local Google Keep. After sending `/note`, your next message is saved as a standalone fleeting note in `01 CONSUME/📥 Inbox/` using your vault's fleeting note template format.

Supported capture types:
- **Text** — saved as-is
- **Voice** — transcribed locally via Whisper, audio file kept alongside the note
- **Image** — saved to inbox, embedded in the note with `![[filename]]`
- **Link** — saved with `type: link` in frontmatter

Notes land as `YYYY-MM-DD HH-MM fleeting.md` and are ready for a later processing pass.

## Local agent API

Flynn exposes a lightweight HTTP API on `127.0.0.1:8765` so other local services can send it events.

```bash
# Health check
curl http://127.0.0.1:8765/health \
  -H "X-Flynn-Secret: your-secret"

# Send a task from another agent
curl -X POST http://127.0.0.1:8765/capture \
  -H "X-Flynn-Secret: your-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Proxmox VM immich is down",
    "domain": "infrastructure",
    "type": "task",
    "notify": true
  }'
```

**Payload fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `text` | yes | The content to capture |
| `type` | no | `task` (default), `note`, or `fleeting` |
| `domain` | no | Skip AI routing and assign directly |
| `notify` | no | Push a Telegram message to you when `true` |

Set `FLYNN_API_SECRET` in `.env` to require authentication. Leave blank to skip.

## Check-ins

Flynn sends structured check-ins at configurable times.

**Morning (default 7:00am):**
1. Sleep & mood (1–5 each)
2. Anything weighing on you?
3. Grateful for?
4. What are you working on today?
5. How does today connect to your mission?

**Evening (default 6:00pm):**
1. Energy (1–5)
2. Wins — what went well?
3. Friction — what was hard?
4. Tomorrow — one thing to carry forward
5. Mission check — did today's work lead back to the sentence?

Numeric scores are parsed and written into the daily note's YAML frontmatter (`sleep`, `mood`, `energy`) so you can query trends across your vault over time.

## Mission alignment

Add your top-level mission sentence to `config.yaml` or directly in `assistant.py`. Flynn surfaces it in every morning and evening check-in as a grounding question. This turns Flynn from a task router into an alignment mirror.

## Configuration

```yaml
vault_path: "~/Documents/your-vault"
default_domain: "self"
timezone: "America/New_York"
api_port: 8765  # local agent API port

checkins:
  morning:
    time: "07:00"
    enabled: true
  evening:
    time: "18:00"
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

Local Ollama routing = $0. Claude API fallback uses the cheapest Haiku model only when Ollama is unavailable. Whisper transcription runs locally. The agent API is local-only. Ongoing cost is electricity.

## Obsidian plugins used

- [Tasks](https://obsidian.md/plugins?id=obsidian-tasks-plugin) — task queries in domain notes
- [Bases](https://obsidian.md/bases) — Flynn dashboard (core plugin, Obsidian 1.8+)

## FLYNN.md — persistent identity

Create `04 META/🤖 Agents/assistant/FLYNN.md` in your vault to give Flynn persistent context. Flynn reads the `## Current Focus` section and uses it in every briefing.

```markdown
## Current Focus
- Launching the new site this week
- Getting class grades submitted

## Notes for Flynn
- "Brighid" routes to family domain
- "initiated" routes to build domain
```

## Changelog

### v0.7 (2026-04-20)
- `/note` command: fleeting note capture — text, voice (Whisper), image, link
- Local agent API on port 8765: `POST /capture`, `GET /health`
- `/cancel` command: exits any active capture session
- Mission sentence added to morning check-in prompt
- `faster-whisper` for local voice transcription (no API cost)

### v0.6
- Mission alignment layer — Q5 added to morning and evening check-ins
- Brighid keyword moved to Family domain

### v0.5
- Simpler morning check-in (combined briefing + prompt)
- Fuzzy score matching for qualitative check-in responses
- All task captures write to daily note only (not a separate inbox file)

### v0.4
- FLYNN.md identity file — persistent context from vault
- Overdue task detection (7+ days flagged in every briefing)
- Weekly digest + `/week` command
- Friday auto-digest

### v0.3
- Intent detection: reflections route to Notes, tasks to Tasks Quick Add
- Date-aware capture: "tomorrow", day names, "this week"
- Structured mental health check-ins with frontmatter score parsing
- `/journal` command

### v0.2
- Daily check-in system (morning and evening)
- Scheduled check-ins

### v0.1
- Initial release: Telegram → Obsidian task capture
- Ollama routing with Claude API and keyword fallbacks
- `/today`, `/status`, `/list`, `/done`, `/focus`

## License

MIT

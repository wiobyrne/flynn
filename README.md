# Flynn

A personal AI assistant bot that connects Telegram to your Obsidian vault. Send it anything — it routes tasks to the right area of your life using local AI (Ollama), no cloud required.

Named after Flynn from Tron.

## What it does

- **Quick capture** — send any message and Flynn routes it to the right domain in your vault
- **Morning briefing** — `/today` gives you focus, domain status, and next actions
- **Task management** — `/list`, `/done`, `/focus` to manage tasks from your phone
- **Local AI routing** — uses Ollama first, falls back to Claude API, then keywords
- **Always on** — runs as a systemd service, survives reboots

## How it works

```
Telegram (your phone)
    ↓
Flynn bot (Python, systemd)
    ↓ classifies via
Ollama (local) → Claude API (fallback) → keywords (fallback)
    ↓ writes to
Obsidian vault (tagged tasks → domain notes → Bases dashboard)
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

cp config.yaml my-config.yaml
# Edit my-config.yaml:
# - Set vault_path to your Obsidian vault
# - Adjust domains to match your life areas
# - Set ollama.model to a model from `ollama list`
```

### 5. Set up your Obsidian vault

Create a folder called `00 DOMAINS` in your vault with one note per domain:

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

Add a Tasks plugin query to each note:
```
```tasks
not done
tags include #domain/self
limit 5
```
```

Create `01 CONSUME/📥 Inbox/Bot Inbox.md` — Flynn writes captured tasks here.

### 6. Run

```bash
venv/bin/python assistant.py
```

### 7. Run as a service (Linux)

```bash
sudo nano /etc/systemd/system/flynn.service
```

```ini
[Unit]
Description=Flynn AI Assistant Bot
After=network.target

[Service]
ExecStart=/path/to/flynn/venv/bin/python /path/to/flynn/assistant.py
WorkingDirectory=/path/to/flynn
Restart=on-failure
User=yourusername

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
| `/today` | Morning briefing — focus, domain status, next actions |
| `/status` | Bar chart of open tasks per domain |
| `/list [domain]` | Show open tasks, optionally filtered by domain |
| `/done <text>` | Mark a matching task complete |
| `/focus <domain> <text>` | Set next action on a domain note |
| Any text | Quick capture — auto-routed to the right domain |

## Configuration

Edit `config.yaml` to:
- Change your vault path
- Add or remove domains
- Adjust keywords for better routing
- Switch Ollama models

Flynn uses the domain `description` field for AI routing and `keywords` as fallback. The more specific your descriptions, the better the routing.

## Cost

Local Ollama routing = $0. Claude API fallback uses `claude-haiku` (cheapest model) only when Ollama is unavailable. For simple task routing, API costs are negligible.

## License

MIT

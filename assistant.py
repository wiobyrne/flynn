#!/usr/bin/env python3
"""
Personal AI Assistant Bot
- Quick capture via Telegram → routes to correct domain in Obsidian vault
- Morning briefing: reads CURRENT_STATE.md + open tasks
- Domain status: counts open tasks per domain
- AI routing: Ollama first, Claude API fallback, keyword fallback
"""

import os
import re
import yaml
import logging
import httpx
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv(Path(__file__).parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

VAULT = Path(config["vault_path"]).expanduser()
OLLAMA_URL = config["ollama"]["url"]
OLLAMA_MODEL = config["ollama"]["model"]
DOMAINS = {d["id"]: d for d in config["domains"]}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # only respond to your chat
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

TIMEZONE = ZoneInfo(config.get("timezone", "America/New_York"))
CHECKIN_STATE: dict[int, str] = {}  # chat_id → "morning" | "evening"
DONE_STATE: dict[int, list] = {}   # chat_id → list of (file, line_num, task_text)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ── AI Routing ────────────────────────────────────────────────────────────────

DOMAIN_DESCRIPTIONS = "\n".join(
    f"- {d['id']}: {d['description']}" for d in config["domains"]
)

CLASSIFY_PROMPT = """Classify the note below into exactly one domain. Reply with ONLY the domain id, nothing else.

Domains:
{domains}

Note: {text}

Domain:"""


async def classify_domain(text: str) -> str:
    prompt = CLASSIFY_PROMPT.format(domains=DOMAIN_DESCRIPTIONS, text=text)

    # 1. Try Ollama
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            result = r.json().get("response", "").strip().lower().split()[0]
            if result in DOMAINS:
                log.info(f"Ollama → {result}")
                return result
    except Exception as e:
        log.warning(f"Ollama unavailable: {e}")

    # 2. Try Claude API (Haiku — cheap and fast)
    if ANTHROPIC_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                result = r.json()["content"][0]["text"].strip().lower().split()[0]
                if result in DOMAINS:
                    log.info(f"Claude → {result}")
                    return result
        except Exception as e:
            log.warning(f"Claude API unavailable: {e}")

    # 3. Keyword fallback
    text_lower = text.lower()
    for domain in config["domains"]:
        if any(kw in text_lower for kw in domain["keywords"]):
            log.info(f"Keyword → {domain['id']}")
            return domain["id"]

    return config.get("default_domain", "build")


# ── Vault Operations ──────────────────────────────────────────────────────────

INBOX_PATH = VAULT / "01 CONSUME" / "📥 Inbox" / "Bot Inbox.md"
INBOX_HEADER = "# Bot Inbox\n\nTasks captured via Telegram. Tagged by domain for Dashboard queries.\n\n"


def save_to_inbox(text: str, domain: str, target_date: date | None = None) -> None:
    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not INBOX_PATH.exists():
        INBOX_PATH.write_text(INBOX_HEADER)
    date_str = (target_date or date.today()).isoformat()
    task_line = f"- [ ] {text} #domain/{domain} 📅 {date_str}\n"
    with INBOX_PATH.open("a") as f:
        f.write(task_line)


def read_active_focus() -> str:
    state_file = VAULT / "04 META" / "🤖 Agents" / "CURRENT_STATE.md"
    if not state_file.exists():
        return "No current state file found."
    content = state_file.read_text()
    match = re.search(r"## Active Focus\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
    return match.group(1).strip() if match else "See CURRENT_STATE.md"


def count_open_tasks() -> dict[str, int]:
    counts = {d: 0 for d in DOMAINS}
    pattern = re.compile(r"- \[ \].*?#domain/(\w+)")
    for md_file in VAULT.rglob("*.md"):
        try:
            for match in pattern.finditer(md_file.read_text()):
                domain = match.group(1)
                if domain in counts:
                    counts[domain] += 1
        except Exception:
            pass
    return counts


def read_domain_next_action(domain_id: str) -> str:
    domain_file = VAULT / "00 DOMAINS" / f"{domain_id.capitalize()}.md"
    if not domain_file.exists():
        return ""
    content = domain_file.read_text()
    match = re.search(r"^next_action:\s*[\"']?(.+?)[\"']?\s*$", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return ""


def get_open_tasks(domain_id: str | None = None) -> list[tuple[Path, int, str]]:
    """Return list of (file, line_number, task_text) for open tasks, optionally filtered by domain."""
    tasks = []
    pattern = re.compile(r"^- \[ \] (.+?)(\s+#domain/(\w+))?(\s+📅.+)?$", re.MULTILINE)
    for md_file in VAULT.rglob("*.md"):
        try:
            content = md_file.read_text()
            for i, line in enumerate(content.splitlines(), 1):
                m = re.match(r"^- \[ \] (.+)$", line)
                if not m:
                    continue
                task_text = m.group(1)
                domain_match = re.search(r"#domain/(\w+)", task_text)
                task_domain = domain_match.group(1) if domain_match else None
                if domain_id is None or task_domain == domain_id:
                    tasks.append((md_file, i, task_text))
        except Exception:
            pass
    return tasks


def mark_task_done(search_text: str) -> tuple[bool, str]:
    """Find a task matching search_text and mark it complete. Returns (success, matched_text)."""
    search_lower = search_text.lower()
    best_file, best_line, best_text = None, None, None
    best_score = 0

    for md_file, line_num, task_text in get_open_tasks():
        # Score by how many search words appear in task
        words = search_lower.split()
        score = sum(1 for w in words if w in task_text.lower())
        if score > best_score:
            best_score = score
            best_file, best_line, best_text = md_file, line_num, task_text

    if not best_file or best_score == 0:
        return False, ""

    lines = best_file.read_text().splitlines(keepends=True)
    lines[best_line - 1] = lines[best_line - 1].replace("- [ ]", "- [x]", 1)
    best_file.write_text("".join(lines))
    return True, best_text


def update_domain_frontmatter(domain_id: str, field: str, value: str) -> bool:
    domain_file = VAULT / "00 DOMAINS" / f"{domain_id.capitalize()}.md"
    if not domain_file.exists():
        return False
    content = domain_file.read_text()
    today = date.today().isoformat()
    # Update the field
    content = re.sub(
        rf"^{field}:.*$", f'{field}: "{value}"', content, flags=re.MULTILINE
    )
    # Update last_updated
    content = re.sub(
        r"^last_updated:.*$", f"last_updated: {today}", content, flags=re.MULTILINE
    )
    domain_file.write_text(content)
    return True


# ── Intent Detection ─────────────────────────────────────────────────────────

_REFLECTION_PATTERNS = [
    r"\bi feel\b", r"\bi'?m feeling\b", r"\bi felt\b",
    r"\btoday was\b", r"\bi noticed\b", r"\bi realized\b",
    r"\bi'?m grateful\b", r"\bgrateful for\b", r"\bthankful\b",
    r"\bi'?m thinking\b", r"\bi'?ve been thinking\b",
    r"\bstress is\b", r"\bfeeling (good|bad|anxious|happy|sad|tired|great|okay|ok|low|high)\b",
]


def detect_intent(text: str) -> str:
    """Return 'reflection' or 'task'. Reflections go to daily notes; tasks go to inbox."""
    lower = text.lower()
    if any(re.search(p, lower) for p in _REFLECTION_PATTERNS):
        return "reflection"
    return "task"


# ── Date Reference Parsing ────────────────────────────────────────────────────

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_date_ref(text: str) -> date:
    """Extract a target date from natural language (today/tomorrow/day name)."""
    lower = text.lower()
    today = date.today()

    if "tomorrow" in lower:
        return today + timedelta(days=1)
    if "today" in lower or "tonight" in lower:
        return today
    if "this week" in lower:
        days_to_friday = (4 - today.weekday()) % 7
        return today + timedelta(days=days_to_friday if days_to_friday > 0 else 7)
    if "next week" in lower:
        return today + timedelta(days=(7 - today.weekday()))
    for i, day in enumerate(_WEEKDAYS):
        if day in lower:
            days_ahead = (i - today.weekday()) % 7
            return today + timedelta(days=days_ahead if days_ahead > 0 else 7)
    return today


# ── Daily Notes ───────────────────────────────────────────────────────────────

DAILY_NOTES_ROOT = VAULT / "03 CREATE" / "Journal" / "Daily"

MORNING_PROMPT = (
    "🌅 *Morning check\\-in*\n\n"
    "Answer however feels natural — I'll save it all\\.\n\n"
    "1\\. *Sleep* — how'd you sleep? \\(1–5\\)\n"
    "2\\. *Mood* — how are you feeling right now? \\(1–5\\)\n"
    "3\\. *Anxiety* — anything weighing on you?\n"
    "4\\. *Grateful* — one thing you're grateful for\n"
    "5\\. *Intention* — what would make today a good day?\n"
)

EVENING_PROMPT = (
    "🌙 *Evening check\\-in*\n\n"
    "Answer however feels natural — I'll save it all\\.\n\n"
    "1\\. *Energy* — how are you ending the day? \\(1–5\\)\n"
    "2\\. *Wins* — what went well?\n"
    "3\\. *Friction* — what was hard or unfinished?\n"
    "4\\. *Tomorrow* — one thing you want to carry forward\n"
)


def get_daily_note_path(d: date) -> Path:
    return DAILY_NOTES_ROOT / d.strftime("%Y/%m") / f"{d.strftime('%Y-%m-%d')}.md"


def create_daily_note_if_missing(d: date) -> Path:
    path = get_daily_note_path(d)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    date_str = d.strftime("%Y-%m-%d")
    week_str = d.strftime("%G-W%V")
    content = f"""---
tags:
  - daily-note
date: {date_str}
week: {week_str}
---

# {date_str}

## Today

```tasks
not done
(due on {date_str}) OR (scheduled on {date_str})
short mode
```

## Tasks Quick Add
- [ ]

---

## Morning Check-in



---

## Evening Check-in



---

## Notes

"""
    path.write_text(content)
    return path


def write_checkin_to_note(d: date, section: str, response: str) -> None:
    """Write a check-in response to the appropriate section of the daily note."""
    path = create_daily_note_if_missing(d)
    content = path.read_text()
    marker = f"## {section} Check-in\n"
    if marker in content:
        timestamp = datetime.now(TIMEZONE).strftime("%H:%M")
        entry = f"{marker}\n*{timestamp}*\n{response}\n"
        if marker + "\n\n" in content:
            content = content.replace(marker + "\n\n", entry, 1)
        elif marker + "\n" in content:
            content = content.replace(marker + "\n", entry, 1)
        path.write_text(content)
    scores = parse_checkin_scores(section, response)
    if scores:
        update_daily_note_frontmatter(d, scores)


def append_task_to_daily_note(text: str, domain: str, target_date: date) -> None:
    """Write a captured task into the Tasks Quick Add section of the target daily note."""
    path = create_daily_note_if_missing(target_date)
    content = path.read_text()
    task_line = f"- [ ] {text} #domain/{domain} 📅 {target_date.isoformat()}\n"
    marker = "## Tasks Quick Add\n"
    if marker in content:
        content = content.replace(marker, marker + task_line, 1)
    else:
        content += f"\n{marker}{task_line}"
    path.write_text(content)


def save_reflection_to_daily_note(text: str) -> None:
    """Append a reflection/journal entry to the Notes section of today's daily note."""
    today = date.today()
    path = create_daily_note_if_missing(today)
    content = path.read_text()
    timestamp = datetime.now(TIMEZONE).strftime("%H:%M")
    entry = f"\n*{timestamp}* {text}\n"
    marker = "## Notes\n"
    if marker in content:
        content = content.replace(marker, marker + entry, 1)
    else:
        content += f"\n{marker}{entry}"
    path.write_text(content)


def parse_checkin_scores(section: str, response: str) -> dict:
    """Extract numeric scores (1–5) from a check-in response."""
    scores = {}
    numbers = re.findall(r"\b([1-5])\b", response)

    if section == "Morning":
        sleep_match = re.search(r"(?:slept?|sleep)[^\d]*([1-5])", response, re.IGNORECASE)
        mood_match = re.search(r"(?:mood|feeling|feel)[^\d]*([1-5])", response, re.IGNORECASE)
        scores["sleep"] = int(sleep_match.group(1)) if sleep_match else (int(numbers[0]) if len(numbers) > 0 else None)
        scores["mood"] = int(mood_match.group(1)) if mood_match else (int(numbers[1]) if len(numbers) > 1 else None)
        scores = {k: v for k, v in scores.items() if v is not None}

    elif section == "Evening":
        energy_match = re.search(r"(?:energy|day)[^\d]*([1-5])", response, re.IGNORECASE)
        scores["energy"] = int(energy_match.group(1)) if energy_match else (int(numbers[0]) if numbers else None)
        scores = {k: v for k, v in scores.items() if v is not None}

    return scores


def update_daily_note_frontmatter(d: date, fields: dict) -> None:
    """Merge fields into the YAML frontmatter of a daily note."""
    path = get_daily_note_path(d)
    if not path.exists():
        return
    content = path.read_text()
    if not content.startswith("---"):
        return
    end = content.find("---", 3)
    if end == -1:
        return
    fm = yaml.safe_load(content[3:end]) or {}
    fm.update(fields)
    new_fm = yaml.dump(fm, default_flow_style=False, allow_unicode=True)
    path.write_text(f"---\n{new_fm}---{content[end+3:]}")


# ── Security: only respond to your own chat ──────────────────────────────────

def is_allowed(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(update.effective_chat.id) == ALLOWED_CHAT_ID


# ── Telegram Handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Assistant ready.\n\n"
        "/today — morning briefing\n"
        "/status — domain task counts\n"
        "/list [domain] — show open tasks\n"
        "/done — mark a task complete (pick from list)\n"
        "/focus <domain> <text> — set domain next action\n"
        "/add <text> — quick capture\n"
        "/journal <text> — save to today's notes\n"
        "\nOr just send any text to capture it."
    )


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text("⏳ Building briefing...")

    today_str = datetime.now().strftime("%A, %B %-d")
    focus = read_active_focus()
    counts = count_open_tasks()

    lines = []
    for d in config["domains"]:
        emoji = d["emoji"]
        label = d["label"]
        count = counts.get(d["id"], 0)
        next_action = read_domain_next_action(d["id"])
        status = f"{count} open" if count else "clear"
        line = f"{emoji} *{label}* — {status}"
        if next_action:
            line += f"\n  → _{next_action}_"
        lines.append(line)

    briefing = (
        f"📋 *{today_str}*\n\n"
        f"*Focus:*\n{focus}\n\n"
        f"*Domains:*\n" + "\n\n".join(lines)
    )
    await update.message.reply_text(briefing, parse_mode="Markdown")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    counts = count_open_tasks()
    lines = []
    for d in config["domains"]:
        count = counts.get(d["id"], 0)
        filled = min(count, 5)
        bar = "█" * filled + "░" * (5 - filled)
        lines.append(f"{d['emoji']} {d['label']:<16} {bar} {count}")
    await update.message.reply_text(
        "```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown"
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    domain_id = ctx.args[0].lower() if ctx.args else None
    if domain_id and domain_id not in DOMAINS:
        await update.message.reply_text(f"Unknown domain. Use: {', '.join(DOMAINS.keys())}")
        return
    tasks = get_open_tasks(domain_id)[:10]
    if not tasks:
        label = DOMAINS[domain_id]["label"] if domain_id else "any domain"
        await update.message.reply_text(f"No open tasks for {label}.")
        return
    label = DOMAINS[domain_id]["label"] if domain_id else "All"
    lines = [f"📋 *{label} — open tasks*\n"]
    for _, _, task_text in tasks:
        # Strip tags and date for cleaner display
        clean = re.sub(r"\s+#domain/\w+", "", task_text)
        clean = re.sub(r"\s+📅\S+", "", clean).strip()
        lines.append(f"• {clean}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    text = " ".join(ctx.args).strip()
    if not text:
        # Show numbered list for selection
        tasks = get_open_tasks()[:10]
        if not tasks:
            await update.message.reply_text("No open tasks found.")
            return
        DONE_STATE[update.effective_chat.id] = tasks
        lines = ["*Open tasks — reply with a number:*\n"]
        for i, (_, _, task_text) in enumerate(tasks, 1):
            clean = re.sub(r"\s+#domain/\w+", "", task_text)
            clean = re.sub(r"\s+📅\S+", "", clean).strip()
            lines.append(f"{i}. {clean}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return
    success, matched = mark_task_done(text)
    if success:
        clean = re.sub(r"\s+#domain/\w+", "", matched)
        clean = re.sub(r"\s+📅\S+", "", clean).strip()
        await update.message.reply_text(f"✓ Done: ~~{clean}~~", parse_mode="Markdown")
    else:
        await update.message.reply_text("No matching task found. Try /done with no args to pick from a list.")


async def cmd_focus(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Usage: /focus <domain> <next action>\n"
            f"Domains: {', '.join(DOMAINS.keys())}"
        )
        return
    domain_id = ctx.args[0].lower()
    action_text = " ".join(ctx.args[1:])
    if domain_id not in DOMAINS:
        await update.message.reply_text(f"Unknown domain. Use: {', '.join(DOMAINS.keys())}")
        return
    success = update_domain_frontmatter(domain_id, "next_action", action_text)
    if success:
        d = DOMAINS[domain_id]
        await update.message.reply_text(
            f"✓ *{d['emoji']} {d['label']}* next action set:\n_{action_text}_",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(f"Could not update {domain_id} — domain note not found.")


async def cmd_journal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("Usage: /journal <your note>")
        return
    save_reflection_to_daily_note(text)
    await update.message.reply_text("✓ Saved to today's notes.", parse_mode="Markdown")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("Usage: /add <your note>")
        return
    await _capture(update, text)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text

    # /done numbered selection
    if chat_id in DONE_STATE:
        tasks = DONE_STATE.pop(chat_id)
        try:
            idx = int(text.strip()) - 1
            if 0 <= idx < len(tasks):
                md_file, line_num, task_text = tasks[idx]
                lines = md_file.read_text().splitlines(keepends=True)
                lines[line_num - 1] = lines[line_num - 1].replace("- [ ]", "- [x]", 1)
                md_file.write_text("".join(lines))
                clean = re.sub(r"\s+#domain/\w+", "", task_text)
                clean = re.sub(r"\s+📅\S+", "", clean).strip()
                await update.message.reply_text(f"✓ Done: ~~{clean}~~", parse_mode="Markdown")
            else:
                await update.message.reply_text("Invalid number. Use /done to try again.")
        except ValueError:
            await update.message.reply_text("Reply with a number. Use /done to try again.")
        return

    # Check-in response takes priority
    if chat_id in CHECKIN_STATE:
        section = CHECKIN_STATE.pop(chat_id)
        write_checkin_to_note(date.today(), section, text)
        await update.message.reply_text(
            f"✓ Saved to *{section} check-in* in today's note.",
            parse_mode="Markdown",
        )
        return

    # Route by intent
    if detect_intent(text) == "reflection":
        save_reflection_to_daily_note(text)
        await update.message.reply_text("✓ Saved to today's notes.", parse_mode="Markdown")
    else:
        await _capture(update, text)


async def scheduled_briefing(ctx) -> None:
    if not ALLOWED_CHAT_ID:
        return
    today_str = datetime.now(TIMEZONE).strftime("%A, %B %-d")
    focus = read_active_focus()
    counts = count_open_tasks()
    lines = []
    for d in config["domains"]:
        count = counts.get(d["id"], 0)
        next_action = read_domain_next_action(d["id"])
        status = f"{count} open" if count else "clear"
        line = f"{d['emoji']} *{d['label']}* — {status}"
        if next_action:
            line += f"\n  → _{next_action}_"
        lines.append(line)
    briefing = (
        f"📋 *{today_str}*\n\n"
        f"*Focus:*\n{focus}\n\n"
        f"*Domains:*\n" + "\n\n".join(lines)
    )
    await ctx.bot.send_message(
        chat_id=int(ALLOWED_CHAT_ID),
        text=briefing,
        parse_mode="Markdown",
    )


async def scheduled_morning_checkin(ctx) -> None:
    if not ALLOWED_CHAT_ID:
        return
    create_daily_note_if_missing(date.today())
    CHECKIN_STATE[int(ALLOWED_CHAT_ID)] = "Morning"
    await ctx.bot.send_message(
        chat_id=int(ALLOWED_CHAT_ID),
        text=MORNING_PROMPT,
        parse_mode="MarkdownV2",
    )


async def scheduled_evening_checkin(ctx) -> None:
    if not ALLOWED_CHAT_ID:
        return
    CHECKIN_STATE[int(ALLOWED_CHAT_ID)] = "Evening"
    await ctx.bot.send_message(
        chat_id=int(ALLOWED_CHAT_ID),
        text=EVENING_PROMPT,
        parse_mode="MarkdownV2",
    )


async def _capture(update: Update, text: str) -> None:
    msg = await update.message.reply_text("⏳ Routing...")
    domain = await classify_domain(text)
    target_date = parse_date_ref(text)
    save_to_inbox(text, domain, target_date)
    append_task_to_daily_note(text, domain, target_date)
    d = DOMAINS[domain]
    today = date.today()
    if target_date == today + timedelta(days=1):
        date_label = " — tomorrow"
    elif target_date != today:
        date_label = f" — {target_date.strftime('%b %-d')}"
    else:
        date_label = ""
    await msg.edit_text(
        f"✓ *{d['emoji']} {d['label']}*{date_label}\n`{text[:100]}`",
        parse_mode="Markdown",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("focus", cmd_focus))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Schedule daily briefing
    briefing_cfg = config.get("briefing", {})
    jq = app.job_queue
    if briefing_cfg.get("enabled", False):
        h, m = map(int, briefing_cfg["time"].split(":"))
        jq.run_daily(
            scheduled_briefing,
            time=dtime(h, m, tzinfo=TIMEZONE),
            name="daily_briefing",
        )
        log.info(f"Daily briefing scheduled at {h:02d}:{m:02d} {config.get('timezone')}")

    # Schedule check-ins
    checkins = config.get("checkins", {})

    if checkins.get("morning", {}).get("enabled", False):
        h, m = map(int, checkins["morning"]["time"].split(":"))
        jq.run_daily(
            scheduled_morning_checkin,
            time=dtime(h, m, tzinfo=TIMEZONE),
            name="morning_checkin",
        )
        log.info(f"Morning check-in scheduled at {h:02d}:{m:02d} {config.get('timezone')}")

    if checkins.get("evening", {}).get("enabled", False):
        h, m = map(int, checkins["evening"]["time"].split(":"))
        jq.run_daily(
            scheduled_evening_checkin,
            time=dtime(h, m, tzinfo=TIMEZONE),
            name="evening_checkin",
        )
        log.info(f"Evening check-in scheduled at {h:02d}:{m:02d} {config.get('timezone')}")

    log.info("Bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

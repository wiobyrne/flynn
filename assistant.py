#!/usr/bin/env python3
"""
Flynn — Personal AI Assistant Bot
- Quick capture via Telegram → routes to correct domain in Obsidian vault
- Tasks written to daily notes; reflections saved to Notes section
- Morning check-in includes compact status; evening wrap-up at 18:00
- AI routing: Ollama first, Claude API fallback, keyword fallback
- Identity and focus read from FLYNN.md in vault
- /note command: fleeting note capture (text, voice, image, link) → 01 CONSUME/📥 Inbox/
"""

import os
import re
import yaml
import logging
import asyncio
import httpx
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
import json
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

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
API_PORT = int(config.get("api_port", 8765))  # local agent API
API_SECRET = os.getenv("FLYNN_API_SECRET", "")  # optional shared secret

TIMEZONE = ZoneInfo(config.get("timezone", "America/New_York"))
CHECKIN_STATE: dict[int, str] = {}  # chat_id → "morning" | "evening"
DONE_STATE: dict[int, list] = {}   # chat_id → list of (file, line_num, task_text)
NOTE_STATE: set[int] = set()        # chat_ids waiting for a fleeting note
PLAN_STATE: set[int] = set()        # chat_ids waiting for a brain-dump
PIN_STATE: set[int] = set()         # chat_ids waiting for a pin message

INBOX_PATH = VAULT / "01 CONSUME" / "📥 Inbox"
INBOX_PATH.mkdir(parents=True, exist_ok=True)

_whisper_model: "WhisperModel | None" = None

def get_whisper_model() -> "WhisperModel | None":
    global _whisper_model
    if not WHISPER_AVAILABLE:
        return None
    if _whisper_model is None:
        log.info("Loading Whisper model (base)...")
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model

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

FLYNN_MD_PATH = VAULT / "04 META" / "🤖 Agents" / "assistant" / "FLYNN.md"


def read_active_focus() -> str:
    """Read current focus from FLYNN.md."""
    if FLYNN_MD_PATH.exists():
        content = FLYNN_MD_PATH.read_text()
        match = re.search(r"## Current Focus\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
        if match:
            lines = [l.strip() for l in match.group(1).strip().splitlines() if l.strip()]
            return "\n".join(lines)
    return "No focus set — edit FLYNN.md to add one."


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


def get_overdue_tasks(days_threshold: int = 7) -> list[tuple[str, str]]:
    """Return (domain, task_text) for open tasks overdue by more than days_threshold days."""
    overdue = []
    today = date.today()
    pattern = re.compile(r"^- \[ \] (.+?) #domain/(\w+) 📅 (\d{4}-\d{2}-\d{2})")
    for md_file in VAULT.rglob("*.md"):
        try:
            for line in md_file.read_text().splitlines():
                m = pattern.match(line)
                if m:
                    task_text, domain, date_str = m.groups()
                    if (today - date.fromisoformat(date_str)).days > days_threshold:
                        overdue.append((domain, task_text))
        except Exception:
            pass
    return overdue


def get_weekly_stats() -> dict:
    """Return captured/completed task counts per domain for the current week."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    stats = {d: {"captured": 0, "completed": 0, "tasks": []} for d in DOMAINS}
    open_pat = re.compile(r"- \[ \] (.+?) #domain/(\w+) 📅 (\d{4}-\d{2}-\d{2})")
    done_pat = re.compile(r"- \[x\] (.+?) #domain/(\w+) 📅 (\d{4}-\d{2}-\d{2})")
    for md_file in VAULT.rglob("*.md"):
        try:
            content = md_file.read_text()
            for m in open_pat.finditer(content):
                text, domain, date_str = m.groups()
                if domain in stats and monday <= date.fromisoformat(date_str) <= today:
                    stats[domain]["captured"] += 1
                    stats[domain]["tasks"].append(text)
            for m in done_pat.finditer(content):
                text, domain, date_str = m.groups()
                if domain in stats and monday <= date.fromisoformat(date_str) <= today:
                    stats[domain]["completed"] += 1
        except Exception:
            pass
    return stats


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
WEEKLY_NOTES_ROOT = VAULT / "03 CREATE" / "Journal" / "Weekly"

EVENING_PROMPT = (
    "🌙 *Evening wrap-up*\n\n"
    "Answer however feels natural.\n\n"
    "1. *Energy* — how are you ending the day? (1–5)\n"
    "2. *Wins* — what went well?\n"
    "3. *Friction* — what was hard or unfinished?\n"
    "4. *Tomorrow* — one thing to carry forward\n"
    "5. *Mission check* — did today's work lead back to the sentence?\n"
    "_Yes / no / partially — and why?_\n"
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


def write_weekly_note(stats: dict, overdue: list) -> Path:
    """Create or update the weekly note for the current week."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    week_str = today.strftime("%G-W%V")

    WEEKLY_NOTES_ROOT.mkdir(parents=True, exist_ok=True)
    path = WEEKLY_NOTES_ROOT / f"{week_str}.md"

    # Build domain table
    table_rows = []
    for d in config["domains"]:
        s = stats.get(d["id"], {})
        captured = s.get("captured", 0)
        completed = s.get("completed", 0)
        table_rows.append(f"| {d['emoji']} {d['label']} | {captured} | {completed} |")

    # Build overdue list
    overdue_lines = ""
    if overdue:
        items = []
        for domain, task_text in overdue:
            d = DOMAINS.get(domain, {})
            clean = re.sub(r"\s+📅\S+", "", task_text).strip()
            items.append(f"- [ ] {clean} #domain/{domain}")
        overdue_lines = "\n".join(items)
    else:
        overdue_lines = "_None — great week!_"

    content = f"""---
tags:
  - weekly-note
week: {week_str}
date_range: {monday.isoformat()} to {sunday.isoformat()}
---

# {week_str} ({monday.strftime("%b %-d")} – {sunday.strftime("%b %-d, %Y")})

## Domain Summary

| Domain | Captured | Completed |
|--------|----------|-----------|
{chr(10).join(table_rows)}

## Overdue Tasks

{overdue_lines}

## Reflections

_What went well this week?_

_What was hard?_

_What do I want to carry into next week?_

## Notes

"""
    path.write_text(content)
    return path


def build_weekly_text(stats: dict, overdue: list) -> str:
    """Build the Telegram summary for the weekly digest."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    week_str = today.strftime("%G-W%V")

    lines = [f"📊 *Week in review — {week_str}*\n"]
    for d in config["domains"]:
        s = stats.get(d["id"], {})
        captured = s.get("captured", 0)
        completed = s.get("completed", 0)
        lines.append(f"{d['emoji']} *{d['label']}* — {captured} captured, {completed} done")

    if overdue:
        lines.append(f"\n⚠️ *{len(overdue)} overdue task(s)* — review in this week's note")

    lines.append("\n_Weekly note created in your vault._")
    return "\n".join(lines)


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


def fuzzy_score(text: str) -> int | None:
    """Map qualitative words to a 1–5 score."""
    lower = text.lower()
    if re.search(r"\b(5|great|excellent|amazing|fantastic|perfect|really good)\b", lower):
        return 5
    if re.search(r"\b(4|good|well|solid|nice|pretty good)\b", lower):
        return 4
    if re.search(r"\b(3|okay|ok|alright|fine|decent|fair|average|so.so|not bad)\b", lower):
        return 3
    if re.search(r"\b(2|low|rough|bad|poor|not great|not good|struggling|tired)\b", lower):
        return 2
    if re.search(r"\b(1|terrible|awful|horrible|exhausted|really bad|miserable)\b", lower):
        return 1
    return None


def parse_checkin_scores(section: str, response: str) -> dict:
    """Extract numeric scores (1–5) from a check-in response.
    Checks for explicit digits first, then X/5 format, then fuzzy word matching.
    Only reads scores from lines containing the relevant keyword."""
    scores = {}

    def score_from_line(line: str, keyword: str) -> int | None:
        if not line or not re.search(keyword, line, re.IGNORECASE):
            return None
        # explicit digit after keyword
        m = re.search(rf"(?:{keyword})[^\d\n]*([1-5])", line, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # X/5 format anywhere on line
        m = re.search(r"([1-5])\s*/\s*5", line)
        if m:
            return int(m.group(1))
        # standalone digit with context (not a list number like "1.")
        m = re.search(r"(?<!\d)([1-5])(?!\s*\.|/)", line)
        if m:
            return int(m.group(1))
        # fuzzy word match
        return fuzzy_score(line)

    lines = response.splitlines()

    if section == "Morning":
        sleep_line = next((l for l in lines if re.search(r"sleep|slept", l, re.IGNORECASE)), "")
        mood_line = next((l for l in lines if re.search(r"mood|feel|feeling", l, re.IGNORECASE)), "")
        s = score_from_line(sleep_line, r"sleep|slept")
        m = score_from_line(mood_line, r"mood|feel|feeling")
        if s: scores["sleep"] = s
        if m: scores["mood"] = m

    elif section == "Evening":
        energy_line = next((l for l in lines if re.search(r"energy|feel|ending", l, re.IGNORECASE)), "")
        e = score_from_line(energy_line, r"energy|feel|ending")
        if e: scores["energy"] = e

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
        "/today — morning briefing + overdue flags\n"
        "/status — domain task counts\n"
        "/list [domain] — show open tasks\n"
        "/done — mark a task complete (pick from list)\n"
        "/focus <domain> <text> — set domain next action\n"
        "/week — weekly digest + create weekly note\n"
        "/add <text> — quick capture\n"
        "/journal <text> — save to today's notes\n"
        "/note — fleeting note (text, voice, image, link → inbox)\n"
        "/plan — morning brain-dump sort\n"
        "/pin — save current task context for later\n"
        "/resume — retrieve last pin\n"
        "/cancel — cancel current session\n"
        "\nOr just send any text to capture it."
    )


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text("⏳ Building briefing...")
    await update.message.reply_text(build_briefing_text(), parse_mode="Markdown")


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


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text("⏳ Building weekly digest...")
    stats = get_weekly_stats()
    overdue = get_overdue_tasks()
    write_weekly_note(stats, overdue)
    await update.message.reply_text(build_weekly_text(stats, overdue), parse_mode="Markdown")


async def scheduled_weekly_digest(ctx) -> None:
    """Runs every day but only sends on Fridays."""
    if date.today().weekday() != 4:
        return
    if not ALLOWED_CHAT_ID:
        return
    stats = get_weekly_stats()
    overdue = get_overdue_tasks()
    write_weekly_note(stats, overdue)
    await ctx.bot.send_message(
        chat_id=int(ALLOWED_CHAT_ID),
        text=build_weekly_text(stats, overdue),
        parse_mode="Markdown",
    )


# ── Fleeting Notes ────────────────────────────────────────────────────────────

def create_fleeting_note(content: str, note_type: str = "text", audio_file: str | None = None) -> Path:
    """Write a fleeting note to the inbox using the vault template format."""
    now = datetime.now(TIMEZONE)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    safe_time = now.strftime("%H-%M")
    filename = f"{date_str} {safe_time} fleeting.md"
    path = INBOX_PATH / filename

    title = content[:60].strip().replace('"', "'")
    audio_line = f"\n**Audio:** [[{audio_file}]]" if audio_file else ""

    body = (
        f"---\n"
        f'title: "{title}"\n'
        f"tags: []\n"
        f"categories: Notes\n"
        f"status: 🌱_seed\n"
        f"dg-publish: false\n"
        f'date: "{date_str}, {time_str}"\n'
        f"shelf: draft\n"
        f"type: {note_type}\n"
        f"---\n\n"
        f"## Quick Note\n"
        f"{content}{audio_line}\n\n"
        f"## Context (Optional)\n\n"
        f"## Next Steps (Optional)\n"
        f"- [ ] Process this into a seed/plant note.\n"
    )
    path.write_text(body)
    return path


def transcribe_audio(ogg_path: Path) -> str | None:
    """Transcribe an audio file using Whisper. Returns text or None."""
    model = get_whisper_model()
    if not model:
        return None
    try:
        segments, _ = model.transcribe(str(ogg_path), beam_size=5)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:
        log.warning(f"Whisper transcription failed: {e}")
        return None


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a fleeting note capture session, or capture inline text immediately."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    inline_text = " ".join(ctx.args).strip()
    if inline_text:
        path = create_fleeting_note(inline_text)
        await update.message.reply_text(
            f"✓ *Fleeting note saved*\n`{path.name}`",
            parse_mode="Markdown",
        )
    else:
        NOTE_STATE.add(chat_id)
        await update.message.reply_text(
            "📝 Send your note — text, voice, image, or link.",
            parse_mode="Markdown",
        )


async def handle_note_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: download OGG, transcribe, save fleeting note."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    if chat_id not in NOTE_STATE:
        await update.message.reply_text("Use /note first to start a capture session.")
        return
    NOTE_STATE.discard(chat_id)

    now = datetime.now(TIMEZONE)
    audio_filename = now.strftime("%Y-%m-%d %H-%M") + " audio.ogg"
    audio_path = INBOX_PATH / audio_filename

    voice = update.message.voice
    tg_file = await ctx.bot.get_file(voice.file_id)
    await tg_file.download_to_drive(str(audio_path))

    msg = await update.message.reply_text("⏳ Transcribing...")
    transcript = transcribe_audio(audio_path)

    if transcript:
        content = transcript
        note_path = create_fleeting_note(content, note_type="voice", audio_file=audio_filename)
        await msg.edit_text(
            f"✓ *Fleeting note saved*\n_{transcript[:120]}_\n`{note_path.name}`",
            parse_mode="Markdown",
        )
    else:
        note_path = create_fleeting_note(
            f"Voice note — transcription unavailable.",
            note_type="voice",
            audio_file=audio_filename,
        )
        await msg.edit_text(
            f"✓ *Audio saved* (transcription unavailable)\n`{note_path.name}`",
            parse_mode="Markdown",
        )


async def handle_note_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages: download image, save fleeting note with embed."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    if chat_id not in NOTE_STATE:
        await update.message.reply_text("Use /note first to start a capture session.")
        return
    NOTE_STATE.discard(chat_id)

    now = datetime.now(TIMEZONE)
    image_filename = now.strftime("%Y-%m-%d %H-%M") + " image.jpg"
    image_path = INBOX_PATH / image_filename

    photo = update.message.photo[-1]  # largest size
    tg_file = await ctx.bot.get_file(photo.file_id)
    await tg_file.download_to_drive(str(image_path))

    caption = update.message.caption or ""
    content = f"![[{image_filename}]]"
    if caption:
        content = f"{caption}\n\n{content}"

    note_path = create_fleeting_note(content, note_type="image")
    await update.message.reply_text(
        f"✓ *Image saved*\n`{note_path.name}`",
        parse_mode="Markdown",
    )


async def handle_note_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text/link messages when in NOTE_STATE."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    if chat_id not in NOTE_STATE:
        return  # fall through to handle_text
    NOTE_STATE.discard(chat_id)

    text = update.message.text
    note_type = "link" if text.startswith("http") else "text"
    note_path = create_fleeting_note(text, note_type=note_type)
    await update.message.reply_text(
        f"✓ *Fleeting note saved*\n`{note_path.name}`",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    cleared = []
    if chat_id in NOTE_STATE:
        NOTE_STATE.discard(chat_id)
        cleared.append("note capture")
    if chat_id in PLAN_STATE:
        PLAN_STATE.discard(chat_id)
        cleared.append("planning session")
    if chat_id in PIN_STATE:
        PIN_STATE.discard(chat_id)
        cleared.append("pin")
    if chat_id in CHECKIN_STATE:
        del CHECKIN_STATE[chat_id]
        cleared.append("check-in")
    if chat_id in DONE_STATE:
        del DONE_STATE[chat_id]
        cleared.append("done selection")
    if cleared:
        await update.message.reply_text(f"✗ Cancelled: {', '.join(cleared)}.")
    else:
        await update.message.reply_text("Nothing active to cancel.")


# ── Planning & Pins ───────────────────────────────────────────────────────────

PINS_PATH = VAULT / "04 META" / "🤖 Agents" / "Pins.md"

PLAN_PROMPT = """You are Flynn, a calm and opinionated executive-function aid helping Ian plan his day.

Mission: "I help educators understand and navigate the digital world — so their students inherit power, not just access."

Domains: Self (health, fitness, wellbeing), Family (marriage, home, kids), Vocation (teaching, research, CofC), Build (newsletter, blog, courses, speaking), Infrastructure (homelab, vault, AI tools)

Your job is NOT just to sort. Your job is to push back.

If Ian lists 8 things as today priorities, tell him that's not a plan — that's a list. Narrow it. Challenge it. Be direct but calm. If something is anxiety dressed as a task, name it. If something doesn't connect to his mission or season of work, say so.

Return exactly these four sections. One line per item. Be concise.

TOP 1-3 TODAY:
(strictly 1-3 items — mission-aligned, realistic given energy, highest cost if deferred. If you can only justify 1, say 1.)

LATER THIS WEEK:
(real commitments, not today)

WAITING / BLOCKED:
(needs someone else or more info — tag these #waiting)

NOT NOW / DEFER:
(mental noise, anxiety, someday, low signal — these do NOT become tasks)

After the sort, add one line: FOCUS — a single sentence on what today is actually about.

Sorting principles:
- Mission alignment and restart cost beat urgency
- Mental noise is not a task
- Energy context matters — match demanding work to available energy
- If something clearly belongs to a domain, note it in parentheses: (vocation), (build), etc.
- Ask ONE clarifying question only if genuinely needed — not as a default

Brain dump:
{text}

Return only the four sections, the FOCUS line, and an optional single question. No preamble. No commentary."""


async def run_plan_sort(text: str) -> str:
    """Send brain-dump to Ollama for sorting. Returns formatted sort or error."""
    prompt = PLAN_PROMPT.format(text=text)
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            return r.json().get("response", "").strip()
    except Exception as e:
        log.warning(f"Ollama plan sort failed: {e}")
        return "⚠️ Ollama unavailable — try again or send tasks manually with /add."


def extract_top_tasks(sort_result: str) -> list[str]:
    """Pull items from the TOP 1-3 TODAY section of the sort result."""
    tasks = []
    in_top = False
    for line in sort_result.splitlines():
        stripped = line.strip()
        if "TOP 1-3 TODAY" in stripped.upper():
            in_top = True
            continue
        if in_top:
            if stripped.startswith("##") or (stripped.isupper() and len(stripped) > 4):
                break
            if stripped.startswith("-") or stripped.startswith("•"):
                tasks.append(stripped.lstrip("-•").strip())
            elif stripped and not stripped.startswith("#"):
                tasks.append(stripped)
    return [t for t in tasks if t]


def write_morning_plan(sort_result: str, raw_dump: str) -> None:
    """Write the morning plan sort to today's daily note."""
    today = date.today()
    path = create_daily_note_if_missing(today)
    content = path.read_text()
    timestamp = datetime.now(TIMEZONE).strftime("%H:%M")

    plan_block = (
        f"\n## Morning Plan\n\n"
        f"*{timestamp}*\n\n"
        f"{sort_result}\n\n"
        f"<details><summary>Raw dump</summary>\n\n{raw_dump}\n\n</details>\n"
    )

    # Append Morning Plan section before Notes
    marker = "## Notes\n"
    if "## Morning Plan\n" in content:
        pass  # already exists, don't duplicate
    elif marker in content:
        content = content.replace(marker, plan_block + "\n" + marker, 1)
        path.write_text(content)
    else:
        path.write_text(content + plan_block)

    # Write Top 1-3 as real tasks
    top_tasks = extract_top_tasks(sort_result)
    for task_text in top_tasks[:3]:
        domain = "build"  # default — classify_domain is async, do synchronously via keyword
        for d in config["domains"]:
            if any(kw in task_text.lower() for kw in d.get("keywords", [])):
                domain = d["id"]
                break
        append_task_to_daily_note(task_text, domain, today)

    # Write waiting items with #waiting tag
    in_waiting = False
    for line in sort_result.splitlines():
        stripped = line.strip()
        if "WAITING" in stripped.upper() and "/" in stripped:
            in_waiting = True
            continue
        if in_waiting:
            if stripped.isupper() and len(stripped) > 4:
                break
            if stripped.startswith("-") or stripped.startswith("•"):
                task_text = stripped.lstrip("-•").strip() + " #waiting"
                append_task_to_daily_note(task_text, "build", today)

    # Write Not Now as a single reflection line
    not_now_items = []
    in_not_now = False
    for line in sort_result.splitlines():
        stripped = line.strip()
        if "NOT NOW" in stripped.upper():
            in_not_now = True
            continue
        if in_not_now:
            if stripped.isupper() and len(stripped) > 4:
                break
            if stripped.startswith("-") or stripped.startswith("•"):
                not_now_items.append(stripped.lstrip("-•").strip())
    if not_now_items:
        save_reflection_to_daily_note("Parked (not now): " + "; ".join(not_now_items))


def write_pin(pin_text: str) -> None:
    """Append a pin entry to Pins.md."""
    now = datetime.now(TIMEZONE)
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    entry = f"\n## {timestamp}\n{pin_text.strip()}\n"
    if not PINS_PATH.exists():
        PINS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PINS_PATH.write_text(
            "---\ntitle: Pins\ntype: system\n---\n\n# Pins\n\n"
            "_Active context switches — use /resume to retrieve the latest._\n"
        )
    content = PINS_PATH.read_text()
    PINS_PATH.write_text(content + entry)


def read_last_pin() -> str | None:
    """Return the most recent pin entry, or None if none exist."""
    if not PINS_PATH.exists():
        return None
    content = PINS_PATH.read_text()
    sections = content.split("\n## ")
    if len(sections) < 2:
        return None
    return "## " + sections[-1].strip()


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a morning planning session — waits for a brain-dump."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    PLAN_STATE.add(chat_id)
    await update.message.reply_text(
        "🧠 *Morning planning*\n\n"
        "Send your brain-dump — everything on your mind for today.\n\n"
        "Include energy level and any context if it's useful. No structure needed.",
        parse_mode="Markdown",
    )


async def handle_plan_dump(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Process a brain-dump sent after /plan."""
    chat_id = update.effective_chat.id
    PLAN_STATE.discard(chat_id)
    text = update.message.text
    msg = await update.message.reply_text("⏳ Sorting...")
    result = await run_plan_sort(text)
    write_morning_plan(result, text)
    top_count = len(extract_top_tasks(result))
    await msg.edit_text(
        f"📋 *Today's plan*\n\n{result}\n\n"
        f"_Saved to today's note — {top_count} task(s) added to Tasks Quick Add._",
        parse_mode="Markdown",
    )


async def cmd_pin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Save current task context for later resumption."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    inline = " ".join(ctx.args).strip()
    if inline:
        write_pin(inline)
        await update.message.reply_text("📌 Pinned.", parse_mode="Markdown")
    else:
        PIN_STATE.add(chat_id)
        await update.message.reply_text(
            "📌 *Pin this task*\n\n"
            "Send your pin in this format:\n\n"
            "*Doing:* what you're working on\n"
            "*Stopped at:* where you are right now\n"
            "*Next:* first action when you return\n"
            "*Blocker:* anything in the way (or none)\n"
            "*Linked to:* note/project/domain (or skip)",
            parse_mode="Markdown",
        )


async def handle_pin_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a pin sent after /pin prompt."""
    chat_id = update.effective_chat.id
    PIN_STATE.discard(chat_id)
    write_pin(update.message.text)
    await update.message.reply_text("📌 Pinned.", parse_mode="Markdown")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Retrieve the most recent pin."""
    if not is_allowed(update):
        return
    pin = read_last_pin()
    if not pin:
        await update.message.reply_text("No pins saved yet.")
        return
    await update.message.reply_text(
        f"📌 *Last pin*\n\n{pin}",
        parse_mode="Markdown",
    )


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

    # Fleeting note capture
    if chat_id in NOTE_STATE:
        await handle_note_text(update, ctx)
        return

    # Planning brain-dump
    if chat_id in PLAN_STATE:
        await handle_plan_dump(update, ctx)
        return

    # Pin message
    if chat_id in PIN_STATE:
        await handle_pin_message(update, ctx)
        return

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


def build_compact_status() -> str:
    """Two-line status for the combined morning message."""
    counts = count_open_tasks()
    overdue = get_overdue_tasks()
    total = sum(counts.values())
    bars = []
    for d in config["domains"]:
        count = counts.get(d["id"], 0)
        filled = min(count, 3)
        bar = d["emoji"] + "█" * filled + "░" * (3 - filled)
        bars.append(bar)
    overdue_str = f" · {len(overdue)} overdue ⚠️" if overdue else ""
    return f"{total} open{overdue_str}\n" + "  ".join(bars)


def build_briefing_text() -> str:
    """Shared logic for /today and the scheduled briefing."""
    today_str = datetime.now().strftime("%A, %B %-d")
    focus = read_active_focus()
    counts = count_open_tasks()
    overdue = get_overdue_tasks()

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

    if overdue:
        overdue_lines = []
        for domain, task_text in overdue[:5]:
            d = DOMAINS.get(domain, {})
            emoji = d.get("emoji", "•")
            clean = re.sub(r"\s+📅\S+", "", task_text).strip()
            overdue_lines.append(f"{emoji} {clean[:60]}")
        briefing += f"\n\n⚠️ *Overdue ({len(overdue)}):*\n" + "\n".join(overdue_lines)
        if len(overdue) > 5:
            briefing += f"\n_...and {len(overdue) - 5} more_"

    return briefing


async def scheduled_briefing(ctx) -> None:
    if not ALLOWED_CHAT_ID:
        return
    await ctx.bot.send_message(
        chat_id=int(ALLOWED_CHAT_ID),
        text=build_briefing_text(),
        parse_mode="Markdown",
    )


async def scheduled_morning_checkin(ctx) -> None:
    if not ALLOWED_CHAT_ID:
        return
    create_daily_note_if_missing(date.today())
    CHECKIN_STATE[int(ALLOWED_CHAT_ID)] = "Morning"
    today_str = datetime.now(TIMEZONE).strftime("%A, %B %-d")
    status = build_compact_status()
    msg = (
        f"📋 *{today_str}*\n{status}\n\n"
        f"🌅 *Morning check-in*\n\n"
        f"1. Sleep & Mood (1–5 each)\n"
        f"2. Anything weighing on you?\n"
        f"3. Grateful for?\n"
        f"4. What are you working on today?\n"
        f"5. How does today's work connect to your mission?\n"
        f"_\"I help educators understand and navigate the digital world — so their students inherit power, not just access.\"_\n"
    )
    await ctx.bot.send_message(
        chat_id=int(ALLOWED_CHAT_ID),
        text=msg,
        parse_mode="Markdown",
    )


async def scheduled_evening_checkin(ctx) -> None:
    if not ALLOWED_CHAT_ID:
        return
    CHECKIN_STATE[int(ALLOWED_CHAT_ID)] = "Evening"
    await ctx.bot.send_message(
        chat_id=int(ALLOWED_CHAT_ID),
        text=EVENING_PROMPT,
        parse_mode="Markdown",
    )


async def _capture(update: Update, text: str) -> None:
    msg = await update.message.reply_text("⏳ Routing...")
    domain = await classify_domain(text)
    target_date = parse_date_ref(text)
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


# ── Local Agent API ───────────────────────────────────────────────────────────
#
# POST http://localhost:8765/capture
# Headers: X-Flynn-Secret: <FLYNN_API_SECRET>  (optional but recommended)
# Body:
#   { "text": "...", "domain": "infrastructure", "type": "task|note|fleeting" }
#
# type "task"     → appended to today's daily note, domain-tagged
# type "note"     → saved to today's ## Notes section (reflection)
# type "fleeting" → new fleeting note in 01 CONSUME/📥 Inbox/
# domain          → optional override; skips AI classification when provided
# notify          → optional bool, sends Telegram message to you when true

_tg_app = None  # set in main(), used by API handlers




def _json_response(writer: asyncio.StreamWriter, data: dict, status: int = 200) -> None:
    body = json.dumps(data).encode()
    reason = "OK" if status == 200 else "Error"
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n".encode() + body
    )


async def _handle_api_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(65536), timeout=10)
        text_raw = raw.decode(errors="replace")
        lines = text_raw.split("\r\n")
        if not lines:
            return
        method, path_str, *_ = (lines[0] + " ").split(" ", 2)

        # Parse headers
        headers: dict[str, str] = {}
        i = 1
        while i < len(lines) and lines[i]:
            if ":" in lines[i]:
                k, v = lines[i].split(":", 1)
                headers[k.strip().lower()] = v.strip()
            i += 1
        body = text_raw.split("\r\n\r\n", 1)[-1] if "\r\n\r\n" in text_raw else ""

        # Auth
        if API_SECRET and headers.get("x-flynn-secret", "") != API_SECRET:
            _json_response(writer, {"error": "unauthorized"}, 401)
            return

        if method == "GET" and path_str == "/health":
            counts = count_open_tasks()
            _json_response(writer, {"status": "ok", "open_tasks": counts})
            return

        if method == "POST" and path_str == "/capture":
            try:
                data = json.loads(body)
            except Exception:
                _json_response(writer, {"error": "invalid JSON"}, 400)
                return

            text = (data.get("text") or "").strip()
            if not text:
                _json_response(writer, {"error": "text required"}, 400)
                return

            note_type = data.get("type", "task")
            domain_override = data.get("domain", "").strip().lower()
            notify = data.get("notify", False)

            if note_type == "fleeting":
                path = create_fleeting_note(text, note_type="agent")
                result: dict = {"saved": "fleeting", "file": path.name}
            elif note_type == "note":
                save_reflection_to_daily_note(text)
                result = {"saved": "reflection"}
            else:
                if domain_override and domain_override in DOMAINS:
                    domain = domain_override
                else:
                    domain = await classify_domain(text)
                target_date = parse_date_ref(text)
                append_task_to_daily_note(text, domain, target_date)
                result = {"saved": "task", "domain": domain, "date": target_date.isoformat()}

            if notify and _tg_app and ALLOWED_CHAT_ID:
                d = DOMAINS.get(result.get("domain", ""), {})
                emoji = d.get("emoji", "🤖")
                await _tg_app.bot.send_message(
                    chat_id=int(ALLOWED_CHAT_ID),
                    text=f"{emoji} *Agent:* {text[:200]}",
                    parse_mode="Markdown",
                )

            log.info(f"API capture: {result}")
            _json_response(writer, result)
            return

        _json_response(writer, {"error": "not found"}, 404)

    except Exception as e:
        log.warning(f"API error: {e}")
        try:
            _json_response(writer, {"error": "internal error"}, 500)
        except Exception:
            pass
    finally:
        await writer.drain()
        writer.close()


async def start_api_server() -> asyncio.Server:
    server = await asyncio.start_server(
        _handle_api_request, "127.0.0.1", API_PORT
    )
    log.info(f"Agent API listening on http://127.0.0.1:{API_PORT}")
    return server


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    global _tg_app
    if not TELEGRAM_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env")

    tg_app = Application.builder().token(TELEGRAM_TOKEN).build()
    _tg_app = tg_app

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("today", cmd_today))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("list", cmd_list))
    tg_app.add_handler(CommandHandler("done", cmd_done))
    tg_app.add_handler(CommandHandler("focus", cmd_focus))
    tg_app.add_handler(CommandHandler("add", cmd_add))
    tg_app.add_handler(CommandHandler("journal", cmd_journal))
    tg_app.add_handler(CommandHandler("week", cmd_week))
    tg_app.add_handler(CommandHandler("note", cmd_note))
    tg_app.add_handler(CommandHandler("cancel", cmd_cancel))
    tg_app.add_handler(CommandHandler("plan", cmd_plan))
    tg_app.add_handler(CommandHandler("pin", cmd_pin))
    tg_app.add_handler(CommandHandler("resume", cmd_resume))
    tg_app.add_handler(MessageHandler(filters.VOICE, handle_note_voice))
    tg_app.add_handler(MessageHandler(filters.PHOTO, handle_note_photo))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Schedule daily briefing
    briefing_cfg = config.get("briefing", {})
    jq = tg_app.job_queue
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

    # Weekly digest — runs daily, fires only on Fridays
    weekly_cfg = config.get("weekly", {})
    if weekly_cfg.get("enabled", True):
        h, m = map(int, weekly_cfg.get("time", "17:00").split(":"))
        jq.run_daily(
            scheduled_weekly_digest,
            time=dtime(h, m, tzinfo=TIMEZONE),
            name="weekly_digest",
        )
        log.info(f"Weekly digest scheduled at {h:02d}:{m:02d} {config.get('timezone')} (Fridays)")

    # Start local agent API server alongside Telegram bot
    api_server = await start_api_server()

    log.info("Bot polling...")
    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        # Run until interrupted
        await asyncio.Event().wait()
        await tg_app.updater.stop()
        await tg_app.stop()

    api_server.close()
    await api_server.wait_closed()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

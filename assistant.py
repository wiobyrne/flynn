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
import asyncio
import httpx
from datetime import date, datetime
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


def save_to_inbox(text: str, domain: str) -> None:
    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not INBOX_PATH.exists():
        INBOX_PATH.write_text(INBOX_HEADER)
    today = date.today().isoformat()
    task_line = f"- [ ] {text} #domain/{domain} 📅 {today}\n"
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
        "/done <text> — mark a task complete\n"
        "/focus <domain> <text> — set domain next action\n"
        "/add <text> — quick capture\n"
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
        await update.message.reply_text("Usage: /done <partial task text>")
        return
    success, matched = mark_task_done(text)
    if success:
        clean = re.sub(r"\s+#domain/\w+", "", matched)
        clean = re.sub(r"\s+📅\S+", "", clean).strip()
        await update.message.reply_text(f"✓ Done: ~~{clean}~~", parse_mode="Markdown")
    else:
        await update.message.reply_text("No matching task found. Try /list to see open tasks.")


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
    await _capture(update, update.message.text)


async def _capture(update: Update, text: str) -> None:
    msg = await update.message.reply_text("⏳ Routing...")
    domain = await classify_domain(text)
    save_to_inbox(text, domain)
    d = DOMAINS[domain]
    await msg.edit_text(
        f"✓ *{d['emoji']} {d['label']}*\n`{text[:100]}`",
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

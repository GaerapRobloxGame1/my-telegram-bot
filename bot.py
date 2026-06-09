# =============================================================
#  Telegram Personal Assistant Bot — v4
#  AI:  Google Gemini 3.5 Flash (google-genai SDK)
#  DB:  Supabase (PostgreSQL)
#
#  SQL — выполни один раз в Supabase SQL Editor:
#  ─────────────────────────────────────────────
#  -- Для хранения памяти пользователя
#  CREATE TABLE IF NOT EXISTS bot_memory (
#    chat_id    TEXT PRIMARY KEY,
#    facts      TEXT DEFAULT '',
#    updated_at TIMESTAMPTZ DEFAULT NOW()
#  );
#
#  -- Для мягких напоминаний (periodic tasks)
#  ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS interval_days   INT  DEFAULT 1;
#  ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS last_sent_date  TEXT;
#  ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS last_reminded   TEXT;
#  ─────────────────────────────────────────────
# =============================================================

import os, asyncio, logging, threading, json, re
from datetime import datetime
import pytz

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from google import genai
from google.genai import types
from supabase import create_client, Client
from flask import Flask

# ─────────────────────── НАСТРОЙКИ ────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s"
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
PORT           = int(os.environ.get("PORT", 5000))
TIMEZONE       = os.environ.get("TIMEZONE", "Europe/Moscow")
TZ             = pytz.timezone(TIMEZONE)
MODEL          = "gemini-3.5-flash"

# ─────────────── БЕЛЫЙ СПИСОК ─────────────────────────────────
_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = set(
    int(x.strip()) for x in _raw.split(",") if x.strip().isdigit()
)

def is_allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS

async def check_access(update: Update) -> bool:
    user = update.effective_user
    if is_allowed(user.id):
        return True
    await update.effective_message.reply_text(
        f"⛔ *Доступ закрыт*\n\n"
        f"Привет, {user.first_name or 'Пользователь'}! Этот бот личный.\n\n"
        f"Твой Telegram ID:\n`{user.id}`\n\nОтправь его владельцу.",
        parse_mode="Markdown"
    )
    log.info(f"BLOCKED  id={user.id}  @{user.username}")
    return False

# ──────────────────────── КЛИЕНТЫ ─────────────────────────────
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai = genai.Client(api_key=GEMINI_API_KEY)

# ─────────────────── FLASK (keep-alive) ───────────────────────
http = Flask(__name__)

@http.route("/")
@http.route("/health")
def health():
    return "Bot is alive! 🤖", 200

def start_flask():
    http.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════

# ── bot_items (напоминания, привычки, заметки, soft_task) ──────
def db_add(chat_id, kind, text, hour=None, minute=0,
           daily=False, interval_days=1):
    row = {
        "chat_id":        str(chat_id),
        "type":           kind,
        "text":           text,
        "remind_hour":    hour,
        "remind_minute":  int(minute or 0),
        "is_daily":       bool(daily),
        "is_active":      True,
        "interval_days":  int(interval_days or 1),
        "last_sent_date": None,
        "last_reminded":  None,
    }
    r = db.table("bot_items").insert(row).execute()
    return r.data[0] if r.data else None

def db_get(chat_id, kind=None):
    q = (db.table("bot_items").select("*")
           .eq("chat_id", str(chat_id))
           .eq("is_active", True))
    if kind:
        q = q.eq("type", kind)
    return q.order("id").execute().data or []

def db_off(item_id, chat_id):
    (db.table("bot_items")
       .update({"is_active": False})
       .eq("id", item_id)
       .eq("chat_id", str(chat_id))
       .execute())

def db_due(hour, minute):
    return (db.table("bot_items").select("*")
              .eq("is_active", True)
              .eq("remind_hour", hour)
              .eq("remind_minute", minute)
              .execute().data or [])

def db_mark_sent(item_id: int, sent_date: str):
    (db.table("bot_items")
       .update({"last_sent_date": sent_date})
       .eq("id", item_id)
       .execute())

# ── bot_memory (постоянная память о пользователе) ──────────────
MEM_LIMIT = 480  # макс символов памяти

def mem_load(chat_id: int) -> str:
    """Загружает факты о пользователе из БД."""
    try:
        r = db.table("bot_memory").select("facts").eq("chat_id", str(chat_id)).execute()
        if r.data:
            return (r.data[0].get("facts") or "").strip()
    except Exception as e:
        log.error(f"mem_load: {e}")
    return ""

def mem_save(chat_id: int, facts: str):
    """Сохраняет обновлённые факты."""
    try:
        (db.table("bot_memory")
           .upsert({"chat_id": str(chat_id), "facts": facts[:MEM_LIMIT],
                    "updated_at": datetime.now(TZ).isoformat()})
           .execute())
    except Exception as e:
        log.error(f"mem_save: {e}")

def mem_add_fact(chat_id: int, new_fact: str):
    """Добавляет новый факт в память, не дублируя существующий."""
    current = mem_load(chat_id)
    if new_fact.lower().strip() in current.lower():
        return  # уже есть
    updated = (current + "\n" + new_fact).strip()
    if len(updated) > MEM_LIMIT:
        # Удаляем старейшие строки
        lines = updated.split("\n")
        while len("\n".join(lines)) > MEM_LIMIT and lines:
            lines.pop(0)
        updated = "\n".join(lines)
    mem_save(chat_id, updated)

def mem_clear(chat_id: int):
    mem_save(chat_id, "")

# ── soft_tasks (мягкие напоминания каждые ~8 часов) ────────────
def _hour_block(dt: datetime) -> str:
    """Блок из 3 в день: 0, 8, 16."""
    if dt.hour < 8:   b = 0
    elif dt.hour < 16: b = 8
    else:              b = 16
    return f"{dt.strftime('%Y-%m-%d')}-{b}"

def soft_task_add(chat_id: int, text: str):
    db_add(chat_id, "soft_task", text)

def soft_task_get_pending(chat_id: int) -> list[str]:
    """
    Возвращает soft_tasks, для которых прошёл новый 8-часовой блок.
    Обновляет last_reminded в БД.
    """
    now   = datetime.now(TZ)
    block = _hour_block(now)

    items = db.table("bot_items").select("*") \
              .eq("chat_id", str(chat_id)) \
              .eq("type", "soft_task") \
              .eq("is_active", True) \
              .execute().data or []

    pending = []
    for item in items:
        last = item.get("last_reminded") or ""
        if last != block:
            pending.append(item["text"])
            try:
                db.table("bot_items").update({"last_reminded": block}) \
                  .eq("id", item["id"]).execute()
            except Exception:
                pass
    return pending

# ══════════════════════════════════════════════════════════════
#  REGEX PRE-PARSER (страховка на случай сбоя AI)
# ══════════════════════════════════════════════════════════════
def _regex_hints(text: str) -> dict:
    result = {}
    t = text.lower()

    if m := re.search(r'\bв\s+(\d{1,2}):(\d{2})\b', t):
        result["hour"], result["minute"] = int(m.group(1)), int(m.group(2))
    elif m := re.search(r'\bв\s+(\d{1,2})\s+ч(?:ас|\.)?', t):
        result["hour"], result["minute"] = int(m.group(1)), 0
    elif m := re.search(r'\bв\s+(\d{1,2})\s+вечера\b', t):
        h = int(m.group(1))
        result["hour"], result["minute"] = (h + 12 if h < 12 else h), 0
    elif m := re.search(r'\bв\s+(\d{1,2})\s+утра\b', t):
        result["hour"], result["minute"] = int(m.group(1)), 0
    elif re.search(r'\bполдень\b', t):
        result["hour"], result["minute"] = 12, 0
    elif re.search(r'\bполночь\b', t):
        result["hour"], result["minute"] = 0, 0

    if re.search(r'через\s+день|каждый\s+второй\s+день|каждые?\s+2\s+дн', t):
        result["interval_days"], result["is_daily"] = 2, True
    elif m := re.search(r'каждые?\s+(\d+)\s+дн|раз\s+в\s+(\d+)\s+дн', t):
        n = int(m.group(1) or m.group(2))
        result["interval_days"], result["is_daily"] = n, True
    elif re.search(r'каждый\s+день|ежедневно', t):
        result["interval_days"], result["is_daily"] = 1, True

    return result

# ══════════════════════════════════════════════════════════════
#  AI — ОСНОВНЫЕ ФУНКЦИИ (google-genai, gemini-3.5-flash)
# ══════════════════════════════════════════════════════════════

PARSE_SYSTEM = """\
Ты — точный парсер команд для Telegram-бота.
Анализируй сообщение и возвращай ТОЛЬКО валидный JSON.

Действия:
• add_reminder  — напомнить в конкретное время
• add_note      — заметка без времени ("запомни", "запиши")
• add_habit     — регулярная привычка ("хочу каждый день X")
• add_memory    — запомнить ФАКТ о пользователе или стиль общения
                  ("запомни что меня зовут", "обращайся ко мне на ты",
                   "запомни что я учусь в...", "общайся без воды")
• add_soft_task — задача с дедлайном, напоминать каждые ~8 ч в ответах
                  ("запомни что нужно сделать ДЗ на среду",
                   "не забудь напомнить мне купить...", "напомни позже про...")
• make_plan     — составить план на день
• show_memory   — показать память ("что ты помнишь", "моя память", "/memory")
• clear_memory  — очистить память ("забудь всё", "/forget")
• list          — список задач
• delete        — удалить задачу
• set_quote     — ежедневные цитаты
• chat          — разговор, вопросы, всё остальное

ВАЖНО: "запомни что нужно X" → add_soft_task (задача)
        "запомни что я / меня зовут / мой стиль / обращайся..." → add_memory (факт)
"""

PARSE_PROMPT = """\
Время: {now}
Сообщение: "{msg}"

JSON:
{{
  "action": "...",
  "text": "суть кратко",
  "hour": null,
  "minute": 0,
  "is_daily": false,
  "interval_days": 1,
  "delete_id": null,
  "quote_example": null,
  "memory_fact": null,
  "plan_activities": null
}}

Время: "в 12:03"→h=12,m=3 | "в 20 часов"→h=20,m=0 | "в 8 вечера"→h=20 | "в полдень"→h=12
Интервал: "через день"→2 | "каждые 3 дня"→3 | "каждый день"→1
По умолчанию: add_reminder без времени→hour=null | add_habit→h=8 | add_note→h=9

add_memory: поле memory_fact = точный факт для сохранения (кратко)
make_plan: поле plan_activities = что хочет сделать (список через запятую)
"""

async def ai_parse(text: str) -> dict:
    """Парсит команду. Возвращает dict с action и параметрами."""
    now    = datetime.now(TZ).strftime("%H:%M %d.%m.%Y")
    prompt = PARSE_PROMPT.format(now=now, msg=text)
    try:
        resp = await asyncio.to_thread(
            ai.models.generate_content,
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=PARSE_SYSTEM,
                response_mime_type="application/json",
                temperature=0.05,
            )
        )
        data = json.loads(resp.text)

        # Regex страховка
        hints = _regex_hints(text)
        if hints:
            act = data.get("action")
            if act in ("chat", "unknown", None) and hints.get("hour") is not None:
                if re.search(r'напомни|напоминай', text.lower()):
                    data["action"] = "add_reminder"
            if hints.get("hour") is not None and data.get("hour") is None and \
               data.get("action") in ("add_reminder", "add_habit"):
                data["hour"]   = hints["hour"]
                data["minute"] = hints.get("minute", 0)
            if hints.get("interval_days", 1) > 1 and data.get("interval_days", 1) <= 1:
                data["interval_days"] = hints["interval_days"]
                data["is_daily"]      = True
            if hints.get("is_daily") and not data.get("is_daily"):
                data["is_daily"] = True
        return data
    except Exception as e:
        log.error(f"ai_parse: {e}")
        hints = _regex_hints(text)
        if hints.get("hour") is not None:
            return {
                "action": "add_reminder",
                "text": text.strip(),
                "hour": hints["hour"], "minute": hints.get("minute", 0),
                "is_daily": hints.get("is_daily", False),
                "interval_days": hints.get("interval_days", 1),
            }
        return {"action": "chat"}


async def ai_reply(text: str, memory: str, soft_tasks: list[str] = None) -> str:
    """
    Ответ на обычное сообщение.
    Память инжектируется как системный контекст (не история чатов).
    """
    system_parts = ["Ты — умный личный ассистент в Telegram. Отвечаешь по-русски."]

    if memory:
        system_parts.append(f"\nЧТО ТЫ ЗНАЕШЬ О ПОЛЬЗОВАТЕЛЕ (следуй этому!):\n{memory}")

    system_parts.append(
        "\nПравила: следуй инструкциям пользователя из его памяти. "
        "Отвечай кратко если не просят длинного ответа. "
        "Можешь говорить о чём угодно: советовать, объяснять, шутить."
    )

    user_msg = text
    if soft_tasks:
        tasks_str = "\n".join(f"• {t}" for t in soft_tasks)
        user_msg = (
            f"{text}\n\n"
            f"[Кстати, мягко напомни в конце ответа про эти задачи пользователя:\n"
            f"{tasks_str}]"
        )

    try:
        resp = await asyncio.to_thread(
            ai.models.generate_content,
            model=MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction="\n".join(system_parts),
                temperature=0.75,
            )
        )
        return resp.text.strip()
    except Exception as e:
        log.error(f"ai_reply: {e}")
        return None  # вернём None → fallback сообщение


async def ai_make_plan(activities: str, memory: str) -> str:
    """Составляет план на день."""
    now = datetime.now(TZ)
    mem_note = f"О пользователе: {memory}\n\n" if memory else ""

    prompt = (
        f"{mem_note}"
        f"Сейчас: {now.strftime('%H:%M, %d.%m.%Y')}\n"
        f"Пользователь хочет сегодня: {activities}\n\n"
        f"Составь умный план на оставшийся день с временны́ми слотами. "
        f"Расставь дела в правильном порядке, учти перерывы и естественный ритм дня. "
        f"Формат каждой строки: 🕒 ЧЧ:ММ — Дело (краткий комментарий)\n"
        f"В конце одна строка с мотивационным итогом.\n"
        f"Будь конкретным, без лишних слов."
    )
    try:
        resp = await asyncio.to_thread(
            ai.models.generate_content,
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.6)
        )
        return resp.text.strip()
    except Exception as e:
        log.error(f"ai_make_plan: {e}")
        return None


async def ai_quote(example: str) -> str:
    """Генерирует вдохновляющую цитату."""
    prompt = (
        f'Создай вдохновляющую цитату на русском в стиле: "{example}". '
        "Верни ТОЛЬКО текст цитаты, без автора и кавычек."
    )
    try:
        resp = await asyncio.to_thread(
            ai.models.generate_content,
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.9)
        )
        return resp.text.strip().strip('"\'')
    except Exception as e:
        log.error(f"ai_quote: {e}")
        return "Каждый день — новый шанс стать лучше!"


# ══════════════════════════════════════════════════════════════
#  ПЛАНИРОВЩИК (жёсткие напоминания)
# ══════════════════════════════════════════════════════════════
async def tick(bot):
    """Каждую минуту — рассылает ❗ напоминания с учётом interval_days."""
    now     = datetime.now(TZ)
    today_s = now.strftime("%Y-%m-%d")
    today_d = now.date()
    items   = db_due(now.hour, now.minute)

    for item in items:
        if item.get("type") == "soft_task":
            continue  # soft_tasks не через планировщик

        interval  = int(item.get("interval_days") or 1)
        last_sent = item.get("last_sent_date")

        if last_sent and interval > 1:
            try:
                last_d     = datetime.strptime(str(last_sent)[:10], "%Y-%m-%d").date()
                days_since = (today_d - last_d).days
                if days_since < interval:
                    continue
            except Exception:
                pass

        cid = int(item["chat_id"])
        try:
            t = item["type"]
            if t == "reminder":
                await bot.send_message(cid, f"❗ Напоминание: {item['text']}")
                db_mark_sent(item["id"], today_s)
                if not item["is_daily"]:
                    db_off(item["id"], cid)
            elif t == "note":
                await bot.send_message(cid, f"❗ Не забудь: {item['text']}")
                db_mark_sent(item["id"], today_s)
            elif t == "habit":
                await bot.send_message(cid, f"❗ Время привычки: {item['text']}")
                db_mark_sent(item["id"], today_s)
            elif t == "quote_config":
                q = await ai_quote(item["text"])
                await bot.send_message(
                    cid, f"✨ *Цитата дня:*\n\n{q}", parse_mode="Markdown"
                )
                db_mark_sent(item["id"], today_s)
        except Exception as e:
            log.error(f"tick item {item.get('id')}: {e}")


# ──────────────── КЛАВИАТУРА ───────────────────────────────────
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Задачи",    callback_data="list"),
            InlineKeyboardButton("☀️ Сегодня",   callback_data="today"),
        ],
        [
            InlineKeyboardButton("🧠 Память",    callback_data="memory"),
            InlineKeyboardButton("📅 План дня",  callback_data="plan"),
        ],
        [
            InlineKeyboardButton("✨ Цитата",    callback_data="quote"),
            InlineKeyboardButton("📖 Помощь",    callback_data="help"),
        ],
    ])


# ────────────────────── ТЕКСТЫ ─────────────────────────────────
WELCOME = (
    "👋 Привет! Я твой личный ассистент на Gemini 3.5 Flash.\n\n"
    "Умею:\n"
    "❗ Напоминать в нужное время\n"
    "🧠 Запоминать факты о тебе и стиль общения\n"
    "📅 Составлять план на день\n"
    "📝 Хранить заметки и мягко напоминать про дедлайны\n"
    "💪 Следить за привычками\n"
    "💬 Просто разговаривать\n\n"
    "*Примеры:*\n"
    "• _Напомни в 12:03 выпить воды_\n"
    "• _Напоминай каждые 2 дня в 20:00 откачать воду_\n"
    "• _Запомни что меня зовут Павел, обращайся дружески_\n"
    "• _Запомни что нужно сделать ДЗ на среду_\n"
    "• _Составь план: отжаться, погулять, сходить в магазин_\n"
)

HELP_TEXT = (
    "📖 *Как пользоваться:*\n\n"
    "❗ *Напоминания:*\n"
    "— Напомни в 12:03 выпить воды\n"
    "— Напоминай каждые 2 дня в 20:00 откачать воду\n"
    "— Каждый день в 7 утра о пробежке\n\n"
    "🧠 *Память (постоянные факты):*\n"
    "— Запомни что меня зовут Павел\n"
    "— Обращайся ко мне на ты, без воды\n"
    "— Запомни что я учусь на третьем курсе в Польше\n"
    "— /memory — посмотреть память\n"
    "— /forget — очистить память\n\n"
    "📋 *Мягкие задачи (напомнит в ответах каждые ~8ч):*\n"
    "— Запомни что нужно сдать отчёт в пятницу\n"
    "— Не забудь напомнить купить подарок маме\n\n"
    "📅 *План дня:*\n"
    "— Составь план: поотжиматься, погулять, магазин\n"
    "— /plan — быстрый план дня\n\n"
    "📝 *Заметки* (каждый день в 9:00):\n"
    "— Запомни купить краску\n\n"
    "💬 *Общение:*\n"
    "— Как дела? / Что думаешь о...?\n\n"
    "📋 /list — список задач\n"
    "☀️ /today — план на сегодня\n"
    "✨ /quote — цитата\n"
    "🗑 /delete [номер] — удалить\n"
    "🔑 /myid — мой Telegram ID\n"
)

FALLBACK = "❌ Бот не смог ответить на это сообщение. Попробуй ещё раз."


# ══════════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ КОМАНД
# ══════════════════════════════════════════════════════════════
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    await u.effective_message.reply_text(
        WELCOME, parse_mode="Markdown", reply_markup=main_keyboard()
    )

async def cmd_help(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    await u.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def cmd_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    await u.effective_message.reply_text("Выбери:", reply_markup=main_keyboard())

async def cmd_myid(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = u.effective_user
    await u.effective_message.reply_text(
        f"🔑 *Твой Telegram ID:*\n`{user.id}`\n\n"
        f"Имя: {user.first_name or '—'}\nUsername: @{user.username or '—'}",
        parse_mode="Markdown"
    )

async def cmd_memory(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid   = u.effective_chat.id
    facts = mem_load(cid)
    if not facts:
        await u.effective_message.reply_text(
            "🧠 Память пуста.\n\n"
            "Попробуй написать: _Запомни что меня зовут Паша_",
            parse_mode="Markdown"
        )
        return
    await u.effective_message.reply_text(
        f"🧠 *Моя память о тебе:*\n\n{facts}\n\n"
        f"_Удалить всё: /forget_",
        parse_mode="Markdown"
    )

async def cmd_forget(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid = u.effective_chat.id
    mem_clear(cid)
    await u.effective_message.reply_text("🧹 Память очищена.")

async def cmd_plan(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid  = u.effective_chat.id
    mem  = mem_load(cid)
    await u.effective_message.reply_chat_action("typing")
    plan = await ai_make_plan("напомни что хочу сделать сегодня (спроси у пользователя)", mem)
    if plan is None:
        await u.effective_message.reply_text(FALLBACK)
        return
    await u.effective_message.reply_text(
        "📅 *Напиши что хочешь сделать сегодня и я составлю план.*\n"
        "_Пример: составь план — поотжиматься, погулять, сходить в технику_",
        parse_mode="Markdown"
    )

async def cmd_list(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = [i for i in db_get(cid) if i.get("type") != "soft_task"]
    if not items:
        await u.effective_message.reply_text("Задач пока нет 😊")
        return

    groups = {
        "reminder":     ("❗ Напоминания",  []),
        "habit":        ("💪 Привычки",     []),
        "note":         ("📝 Заметки",      []),
        "quote_config": ("✨ Цитаты",       []),
    }
    for it in items:
        k = it.get("type", "reminder")
        if k in groups:
            groups[k][1].append(it)

    # Soft tasks отдельно
    soft = db_get(cid, "soft_task")

    lines = ["📋 *Твои задачи:*\n"]
    for _, (label, grp) in groups.items():
        if not grp: continue
        lines.append(f"*{label}:*")
        for it in grp:
            h  = it.get("remind_hour")
            m  = it.get("remind_minute", 0)
            iv = int(it.get("interval_days") or 1)
            ts = f" _{h:02d}:{m:02d}_" if h is not None else ""
            ds = (f" _(каждые {iv} дн.)_" if iv > 1 else
                  " _(ежедн.)_" if it.get("is_daily") else "")
            lines.append(f"  `{it['id']}` — {it['text']}{ts}{ds}")
        lines.append("")

    if soft:
        lines.append("*📌 Мягкие напоминания:*")
        for it in soft:
            lines.append(f"  `{it['id']}` — {it['text']}")
        lines.append("")

    lines.append("_Удалить: /delete [номер]_")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_today(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = [i for i in db_get(cid) if i.get("type") != "soft_task"]
    if not items:
        await u.effective_message.reply_text("Задач нет. Отличный день! ☀️")
        return

    now   = datetime.now(TZ)
    emoji = {"reminder": "❗", "habit": "💪", "note": "📝", "quote_config": "✨"}
    timed = sorted(
        [i for i in items if i.get("remind_hour") is not None],
        key=lambda x: (x["remind_hour"], x.get("remind_minute", 0))
    )
    untimed = [i for i in items if i.get("remind_hour") is None]

    lines = [f"☀️ *План на {now.strftime('%d.%m.%Y')}:*\n"]
    for it in timed:
        h, m = it["remind_hour"], it.get("remind_minute", 0)
        iv   = int(it.get("interval_days") or 1)
        e    = emoji.get(it["type"], "•")
        sfx  = f" _(каждые {iv} дн.)_" if iv > 1 else ""
        lines.append(f"{e} *{h:02d}:{m:02d}* — {it['text']}{sfx}")
    if untimed:
        lines.append("\n📌 *Без времени:*")
        for it in untimed:
            lines.append(f"  {emoji.get(it['type'],'•')} {it['text']}")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_delete(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid  = u.effective_chat.id
    args = ctx.args
    if not args or not args[0].isdigit():
        await u.effective_message.reply_text(
            "Укажи номер: `/delete 5`\nСписок: /list", parse_mode="Markdown"
        )
        return
    db_off(int(args[0]), cid)
    await u.effective_message.reply_text(f"✅ Задача #{args[0]} удалена!")

async def cmd_quote(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid     = u.effective_chat.id
    configs = db_get(cid, "quote_config")
    example = configs[0]["text"] if configs else "Каждый день — шанс стать лучше"
    await u.effective_message.reply_chat_action("typing")
    q = await ai_quote(example)
    await u.effective_message.reply_text(
        f"✨ *Цитата:*\n\n{q}", parse_mode="Markdown"
    )

# ─────────────── CALLBACK ─────────────────────────────────────
async def handle_callback(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query
    await query.answer()

    if query.data == "myid":
        await cmd_myid(u, ctx); return
    if not await check_access(u): return

    dispatch = {
        "list":   cmd_list,
        "today":  cmd_today,
        "quote":  cmd_quote,
        "help":   cmd_help,
        "memory": cmd_memory,
        "plan":   cmd_plan,
    }
    handler = dispatch.get(query.data)
    if handler:
        await handler(u, ctx)

# ══════════════════════════════════════════════════════════════
#  ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ══════════════════════════════════════════════════════════════
async def handle_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return

    cid  = u.effective_chat.id
    text = u.message.text
    await u.message.reply_chat_action("typing")

    # Парсим намерение
    intent        = await ai_parse(text)
    action        = intent.get("action") or "chat"
    content       = (intent.get("text") or "").strip() or text.strip()
    hour          = intent.get("hour")
    minute        = int(intent.get("minute") or 0)
    daily         = bool(intent.get("is_daily", False))
    interval_days = int(intent.get("interval_days") or 1)

    # ── Напоминание ──────────────────────────────────────────────
    if action == "add_reminder":
        if hour is None:
            await u.message.reply_text(
                "❓ В какое время напомнить?\n"
                "_Пример: Напомни в 18:00 почистить зубы_",
                parse_mode="Markdown"
            )
            return
        db_add(cid, "reminder", content, int(hour), minute, daily, interval_days)
        freq = (f"каждые {interval_days} дн." if interval_days > 1 else
                "каждый день" if daily else "один раз")
        await u.message.reply_text(
            f"✅ Запомнил!\n⏰ *{int(hour):02d}:{minute:02d}* — {freq}\n📌 _{content}_",
            parse_mode="Markdown"
        )

    # ── Заметка ──────────────────────────────────────────────────
    elif action == "add_note":
        db_add(cid, "note", content, 9, 0, True)
        await u.message.reply_text(
            f"📝 Записал! Буду напоминать каждый день в *9:00*:\n_{content}_",
            parse_mode="Markdown"
        )

    # ── Привычка ─────────────────────────────────────────────────
    elif action == "add_habit":
        h = int(hour or 8)
        db_add(cid, "habit", content, h, minute, True, interval_days)
        freq = f"каждые {interval_days} дн." if interval_days > 1 else "каждый день"
        await u.message.reply_text(
            f"💪 Привычка добавлена!\n⏰ *{h:02d}:{minute:02d}* — {freq}\n📌 _{content}_",
            parse_mode="Markdown"
        )

    # ── Память — новый факт ───────────────────────────────────────
    elif action == "add_memory":
        fact = (intent.get("memory_fact") or content).strip()
        if fact:
            mem_add_fact(cid, fact)
            await u.message.reply_text(
                f"🧠 Запомнил!\n_{fact}_\n\nПосмотреть всё: /memory",
                parse_mode="Markdown"
            )
        else:
            await u.message.reply_text("🤔 Не понял что запомнить. Попробуй точнее.")

    # ── Мягкая задача (periodic reminder) ───────────────────────
    elif action == "add_soft_task":
        soft_task_add(cid, content)
        await u.message.reply_text(
            f"📌 Запомнил задачу!\n_{content}_\n\n"
            f"Буду мягко напоминать тебе об этом раз в ~8 часов в ответах.",
            parse_mode="Markdown"
        )

    # ── Показать память ──────────────────────────────────────────
    elif action == "show_memory":
        await cmd_memory(u, ctx)

    # ── Очистить память ──────────────────────────────────────────
    elif action == "clear_memory":
        mem_clear(cid)
        await u.message.reply_text("🧹 Память очищена.")

    # ── Список ───────────────────────────────────────────────────
    elif action == "list":
        await cmd_list(u, ctx)

    # ── Удаление ─────────────────────────────────────────────────
    elif action == "delete":
        did = intent.get("delete_id")
        if did:
            db_off(int(did), cid)
            await u.message.reply_text(f"✅ Задача #{did} удалена!")
        else:
            await u.message.reply_text(
                "Укажи номер: _удали номер 5_\nСписок: /list",
                parse_mode="Markdown"
            )

    # ── Цитаты ───────────────────────────────────────────────────
    elif action == "set_quote":
        example = intent.get("quote_example") or content or "Каждый день — новый шанс"
        for old in db_get(cid, "quote_config"):
            db_off(old["id"], cid)
        db_add(cid, "quote_config", example, 8, 0, True)
        await u.message.reply_text(
            f"✨ Буду присылать цитаты каждый день в *8:00*!\nСтиль: _{example}_",
            parse_mode="Markdown"
        )

    # ── План дня ─────────────────────────────────────────────────
    elif action == "make_plan":
        activities = intent.get("plan_activities") or content
        mem        = mem_load(cid)
        plan       = await ai_make_plan(activities, mem)
        if plan is None:
            await u.message.reply_text(FALLBACK)
            return
        await u.message.reply_text(f"📅 *План на сегодня:*\n\n{plan}", parse_mode="Markdown")

    # ── Обычный чат ──────────────────────────────────────────────
    else:
        mem         = mem_load(cid)
        soft_tasks  = soft_task_get_pending(cid)
        reply       = await ai_reply(text, mem, soft_tasks if soft_tasks else None)
        if reply is None:
            await u.message.reply_text(FALLBACK)
            return
        await u.message.reply_text(reply)


# ──────────────── РЕГИСТРАЦИЯ КОМАНД ──────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "👋 Начало работы"),
        BotCommand("menu",   "📋 Быстрое меню"),
        BotCommand("list",   "📋 Все задачи"),
        BotCommand("today",  "☀️ План на сегодня"),
        BotCommand("plan",   "📅 Составить план дня"),
        BotCommand("memory", "🧠 Моя память"),
        BotCommand("forget", "🧹 Очистить память"),
        BotCommand("quote",  "✨ Получить цитату"),
        BotCommand("delete", "🗑 Удалить задачу"),
        BotCommand("myid",   "🔑 Мой Telegram ID"),
        BotCommand("help",   "📖 Помощь"),
    ])
    log.info("✅ Bot commands registered")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
async def main():
    threading.Thread(target=start_flask, daemon=True).start()
    log.info(f"Flask started on :{PORT}")

    if ALLOWED_USERS:
        log.info(f"🔒 Restricted to IDs: {ALLOWED_USERS}")
    else:
        log.info("🌐 Open to all users")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("menu",   cmd_menu))
    app.add_handler(CommandHandler("myid",   cmd_myid))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("today",  cmd_today))
    app.add_handler(CommandHandler("plan",   cmd_plan))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("quote",  cmd_quote))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    sched = AsyncIOScheduler(timezone=TZ)
    sched.add_job(
        tick, IntervalTrigger(minutes=1),
        args=[app.bot], id="tick", max_instances=1
    )
    sched.start()
    log.info("Scheduler started")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info(f"✅ Bot running on {MODEL}")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        sched.shutdown()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

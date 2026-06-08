# =============================================================
#  Telegram Personal Assistant Bot — v3
#  AI: Google Gemini 3.5 Flash
#      • parse_model  — строгий JSON, temperature=0.05
#      • chat_model   — свободный разговор, история, temperature=0.8
#  DB: Supabase (PostgreSQL)
#  Host: Render.com + UptimeRobot
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
import google.generativeai as genai
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

# ─────────────── БЕЛЫЙ СПИСОК ПОЛЬЗОВАТЕЛЕЙ ───────────────────
# В .env: ALLOWED_USERS=123456789,987654321
# Если пусто — бот открыт для всех
_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = set(
    int(x.strip()) for x in _raw.split(",") if x.strip().isdigit()
)

def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS

async def check_access(update: Update) -> bool:
    user = update.effective_user
    if is_allowed(user.id):
        return True
    await update.effective_message.reply_text(
        f"⛔ *Доступ закрыт*\n\n"
        f"Привет, {user.first_name or 'Пользователь'}! Этот бот личный.\n\n"
        f"Твой Telegram ID:\n`{user.id}`\n\n"
        f"Отправь его владельцу бота.",
        parse_mode="Markdown"
    )
    log.info(f"BLOCKED  id={user.id}  name={user.first_name}  @{user.username}")
    return False

# ──────────────────────── КЛИЕНТЫ ─────────────────────────────
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─────────────────── FLASK (keep-alive) ───────────────────────
http = Flask(__name__)

@http.route("/")
@http.route("/health")
def health():
    return "Bot is alive! 🤖", 200

def start_flask():
    http.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ──────────────────────── БАЗА ДАННЫХ ─────────────────────────
# Если не делал раньше, выполни в Supabase SQL Editor:
#   ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS interval_days  INT  DEFAULT 1;
#   ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS last_sent_date DATE;

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

# ═══════════════════════════════════════════════════════════════
#  ИИ — НАСТРОЙКА
# ═══════════════════════════════════════════════════════════════
genai.configure(api_key=GEMINI_API_KEY)

# ── МОДЕЛЬ 1: Парсер команд ─────────────────────────────────────
# response_mime_type="application/json" — ключевая фича:
# модель физически не может вернуть сломанный JSON.
# temperature=0.05 — максимально детерминированный вывод.

PARSE_SYSTEM = """\
Ты — точный парсер команд для Telegram-бота с напоминаниями.
Анализируй сообщение пользователя и возвращай ТОЛЬКО валидный JSON без пояснений.

Доступные действия:
• add_reminder — напомнить в конкретное время (есть слово "напомни" или указано время)
• add_note     — запомнить заметку ("запомни"/"запиши", без конкретного времени)
• add_habit    — регулярная привычка ("хочу каждый день X", "привычка")
• list         — показать задачи ("список", "что есть", "покажи всё")
• delete       — удалить задачу ("удали номер N", "убери задачу N")
• set_quote    — ежедневные вдохновляющие цитаты
• chat         — ВСЁ остальное: разговор, вопросы, приветствия, советы, мнения

ВАЖНО: Используй "chat" для: "как дела", "привет", любых вопросов не про задачи,
просьб объяснить что-то, шуток, мнений, советов.
"""

PARSE_PROMPT = """\
Время сейчас: {now}
Сообщение пользователя: "{msg}"

Верни JSON строго в таком формате:
{{
  "action": "add_reminder|add_note|add_habit|list|delete|set_quote|chat",
  "text": "суть задачи кратко (пусто если chat)",
  "hour": null,
  "minute": 0,
  "is_daily": false,
  "interval_days": 1,
  "delete_id": null,
  "quote_example": null
}}

═══ ПРАВИЛА ВРЕМЕНИ ═══
• "в 12:03"             → hour=12, minute=3
• "в 20:00" / "в 20 часов" / "в 20 ч" → hour=20, minute=0
• "в 8 вечера"          → hour=20, minute=0  (pm: +12 если < 12)
• "в 9 утра"            → hour=9,  minute=0
• "в полдень"           → hour=12, minute=0
• "в полночь"           → hour=0,  minute=0
• время не указано      → hour=null

═══ ПРАВИЛА ИНТЕРВАЛОВ ═══
• "каждый день" / "ежедневно"              → is_daily=true, interval_days=1
• "через день" / "каждый второй день"      → is_daily=true, interval_days=2
• "каждые 3 дня" / "раз в 3 дня"          → is_daily=true, interval_days=3
• "каждые N дней" / "раз в N дней"        → is_daily=true, interval_days=N

═══ ВРЕМЯ ПО УМОЛЧАНИЮ ═══
• add_reminder без времени → hour=null (спросим у пользователя)
• add_habit без времени    → hour=8,   minute=0
• add_note                 → hour=9,   minute=0 (не меняется)

═══ ПРИМЕРЫ ═══
"напомни в 12:03 выпить воды"
→ {{"action":"add_reminder","text":"Выпить воды","hour":12,"minute":3,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null}}

"напоминай каждые 2 дня в 20:00 откачать воду"
→ {{"action":"add_reminder","text":"Откачать воду","hour":20,"minute":0,"is_daily":true,"interval_days":2,"delete_id":null,"quote_example":null}}

"хочу каждый день делать зарядку в 7 утра"
→ {{"action":"add_habit","text":"Делать зарядку","hour":7,"minute":0,"is_daily":true,"interval_days":1,"delete_id":null,"quote_example":null}}

"как дела?" / "привет" / "что думаешь о погоде?"
→ {{"action":"chat","text":"","hour":null,"minute":0,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null}}
"""

parse_model = genai.GenerativeModel(
    model_name="gemini-3.5-flash",
    system_instruction=PARSE_SYSTEM,
    generation_config=genai.GenerationConfig(
        temperature=0.05,
        response_mime_type="application/json",   # 🔑 всегда валидный JSON
    )
)

# ── МОДЕЛЬ 2: Чат-ассистент ─────────────────────────────────────
# Отдельная модель для свободного разговора с памятью истории.

CHAT_SYSTEM = """\
Ты — умный и дружелюбный личный ассистент в Telegram. 
Отвечаешь по-русски — живо, естественно, как хороший друг.
Можешь говорить о чём угодно: отвечать на вопросы, давать советы, 
обсуждать любые темы, шутить.
Держи ответы краткими (2–4 предложения), если не просят длинный ответ.
Никогда не говори "я не могу" для обычных вопросов — просто отвечай.
"""

chat_model = genai.GenerativeModel(
    model_name="gemini-3.5-flash",
    system_instruction=CHAT_SYSTEM,
    generation_config=genai.GenerationConfig(temperature=0.8)
)

# ── МОДЕЛЬ 3: Цитаты ────────────────────────────────────────────
quote_model = genai.GenerativeModel(model_name="gemini-3.5-flash")

# ── ИСТОРИЯ РАЗГОВОРА (per user, в памяти) ──────────────────────
_chat_history: dict[int, list[str]] = {}
MAX_HISTORY_PAIRS = 8  # последних 8 пар реплик = 16 строк

def _get_context(user_id: int) -> str:
    return "\n".join(_chat_history.get(user_id, []))

def _save_to_history(user_id: int, user_msg: str, bot_reply: str):
    hist = _chat_history.setdefault(user_id, [])
    hist.append(f"Пользователь: {user_msg}")
    hist.append(f"Ты: {bot_reply}")
    _chat_history[user_id] = hist[-(MAX_HISTORY_PAIRS * 2):]

# ═══════════════════════════════════════════════════════════════
#  REGEX PRE-PARSER — резервный разбор времени и интервалов
#  Работает независимо от AI, гарантирует корректность данных.
# ═══════════════════════════════════════════════════════════════
def _regex_hints(text: str) -> dict:
    """Извлекает время и интервал из текста регулярками."""
    result = {}
    t = text.lower()

    # ── Время ──
    # "в 12:03" / "в 12:03 "
    if m := re.search(r'\bв\s+(\d{1,2}):(\d{2})\b', t):
        result["hour"]   = int(m.group(1))
        result["minute"] = int(m.group(2))
    # "в 20 часов" / "в 20 ч" / "в 8 час"
    elif m := re.search(r'\bв\s+(\d{1,2})\s+ч(?:ас|\.)?', t):
        result["hour"]   = int(m.group(1))
        result["minute"] = 0
    # "в 8 вечера" → +12
    elif m := re.search(r'\bв\s+(\d{1,2})\s+вечера\b', t):
        h = int(m.group(1))
        result["hour"]   = (h + 12) if h < 12 else h
        result["minute"] = 0
    # "в 9 утра"
    elif m := re.search(r'\bв\s+(\d{1,2})\s+утра\b', t):
        result["hour"]   = int(m.group(1))
        result["minute"] = 0
    # "в полдень"
    elif re.search(r'\bполдень\b', t):
        result["hour"]   = 12
        result["minute"] = 0
    # "в полночь"
    elif re.search(r'\bполночь\b', t):
        result["hour"]   = 0
        result["minute"] = 0

    # ── Интервал ──
    # "через день" / "каждый второй день"
    if re.search(r'через\s+день|каждый\s+второй\s+день|каждые?\s+2\s+дн', t):
        result["interval_days"] = 2
        result["is_daily"]      = True
    # "каждые N дней" / "раз в N дней"
    elif m := re.search(r'каждые?\s+(\d+)\s+дн|раз\s+в\s+(\d+)\s+дн', t):
        n = int(m.group(1) or m.group(2))
        result["interval_days"] = n
        result["is_daily"]      = True
    # "каждый день" / "ежедневно"
    elif re.search(r'каждый\s+день|ежедневно', t):
        result["interval_days"] = 1
        result["is_daily"]      = True

    return result


# ═══════════════════════════════════════════════════════════════
#  ОСНОВНЫЕ AI-ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════

async def ai_parse(text: str) -> dict:
    """
    Парсит сообщение пользователя.
    1. AI (gemini-3.5-flash + JSON mode) → структурированный intent
    2. Regex hints накладываются поверх для надёжности
    3. Полный regex-fallback если AI упал
    """
    now    = datetime.now(TZ).strftime("%H:%M %d.%m.%Y")
    prompt = PARSE_PROMPT.format(now=now, msg=text)

    try:
        resp = await asyncio.to_thread(parse_model.generate_content, prompt)
        data = json.loads(resp.text)  # response_mime_type гарантирует корректный JSON

        # Наложение regex-подсказок как страховка
        hints = _regex_hints(text)
        if hints:
            # Исправляем missed action
            if data.get("action") in ("chat", "unknown", None) and hints.get("hour") is not None:
                # Если AI решил это chat, но явно указано время — скорее reminder
                if re.search(r'напомни|напоминай|скажи|буди', text.lower()):
                    data["action"] = "add_reminder"

            # Исправляем пропущенное время
            if hints.get("hour") is not None and data.get("hour") is None and \
               data.get("action") in ("add_reminder", "add_habit"):
                data["hour"]   = hints["hour"]
                data["minute"] = hints.get("minute", 0)

            # Исправляем интервал
            if hints.get("interval_days", 1) > 1 and data.get("interval_days", 1) == 1:
                data["interval_days"] = hints["interval_days"]
                data["is_daily"]      = True
            if hints.get("is_daily") and not data.get("is_daily"):
                data["is_daily"] = True

        return data

    except Exception as e:
        log.error(f"ai_parse error: {e}")
        # Полный fallback на regex — работает даже без интернета
        hints = _regex_hints(text)
        if hints.get("hour") is not None:
            is_cmd = bool(re.search(r'напомни|напоминай', text.lower()))
            return {
                "action":        "add_reminder" if is_cmd else "add_reminder",
                "text":          text.strip(),
                "hour":          hints["hour"],
                "minute":        hints.get("minute", 0),
                "is_daily":      hints.get("is_daily", False),
                "interval_days": hints.get("interval_days", 1),
                "delete_id":     None,
                "quote_example": None,
            }
        return {"action": "chat"}


async def ai_chat_reply(user_id: int, text: str) -> str:
    """Генерирует ответ на обычное сообщение с историей диалога."""
    context = _get_context(user_id)
    prompt  = (f"История разговора:\n{context}\n\n" if context else "") + \
              f"Пользователь: {text}"
    try:
        resp  = await asyncio.to_thread(chat_model.generate_content, prompt)
        reply = resp.text.strip()
        _save_to_history(user_id, text, reply)
        return reply
    except Exception as e:
        log.error(f"ai_chat_reply error: {e}")
        return "Прости, что-то пошло не так 😕 Попробуй ещё раз."


async def ai_quote(example: str) -> str:
    """Генерирует вдохновляющую цитату."""
    prompt = (
        f'Создай вдохновляющую цитату на русском языке в стиле: "{example}". '
        "Верни ТОЛЬКО текст цитаты, без автора и кавычек."
    )
    try:
        resp = await asyncio.to_thread(quote_model.generate_content, prompt)
        return resp.text.strip().strip('"').strip("'")
    except Exception as e:
        log.error(f"ai_quote: {e}")
        return "Каждый день — новый шанс стать лучше!"


# ──────────────────────── ПЛАНИРОВЩИК ─────────────────────────
async def tick(bot):
    """Каждую минуту рассылает запланированное, учитывает interval_days."""
    now     = datetime.now(TZ)
    today_s = now.strftime("%Y-%m-%d")
    today_d = now.date()
    items   = db_due(now.hour, now.minute)

    for item in items:
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
                await bot.send_message(cid, f"⏰ Напоминание: {item['text']}")
                db_mark_sent(item["id"], today_s)
                if not item["is_daily"]:
                    db_off(item["id"], cid)
            elif t == "note":
                await bot.send_message(cid, f"📝 Не забудь: {item['text']}")
                db_mark_sent(item["id"], today_s)
            elif t == "habit":
                await bot.send_message(cid, f"💪 Время привычки: {item['text']}")
                db_mark_sent(item["id"], today_s)
            elif t == "quote_config":
                q = await ai_quote(item["text"])
                await bot.send_message(
                    cid, f"✨ *Цитата дня:*\n\n{q}", parse_mode="Markdown"
                )
                db_mark_sent(item["id"], today_s)
        except Exception as e:
            log.error(f"tick item {item.get('id')}: {e}")


# ──────────────── КЛАВИАТУРА БЫСТРЫХ ДЕЙСТВИЙ ─────────────────
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Мои задачи",      callback_data="list"),
            InlineKeyboardButton("☀️ На сегодня",      callback_data="today"),
        ],
        [
            InlineKeyboardButton("✨ Цитата",           callback_data="quote"),
            InlineKeyboardButton("📖 Помощь",           callback_data="help"),
        ],
        [
            InlineKeyboardButton("🔑 Мой Telegram ID",  callback_data="myid"),
        ],
    ])


# ────────────────────── ТЕКСТЫ ─────────────────────────────────
WELCOME = (
    "👋 Привет! Я твой личный ассистент на Gemini 3.5 Flash.\n\n"
    "Умею:\n"
    "⏰ Напоминать в нужное время\n"
    "📝 Хранить заметки\n"
    "💪 Следить за привычками (каждый день / раз в N дней)\n"
    "✨ Присылать вдохновляющие цитаты\n"
    "💬 Просто разговаривать о чём угодно\n\n"
    "*Пиши естественным языком:*\n"
    "• _Напомни в 12:03 выпить воды_\n"
    "• _Напоминай каждые 2 дня в 20:00 откачать воду_\n"
    "• _Каждый день в 7 утра напоминай о пробежке_\n"
    "• _Как дела?_ — просто поговори 😊\n"
)

HELP_TEXT = (
    "📖 *Как пользоваться:*\n\n"
    "⏰ *Напоминания:*\n"
    "— Напомни в 12:03 выпить воды\n"
    "— Напомни в 20 часов проверить задачи\n"
    "— Каждый день в 7 утра напоминай о пробежке\n\n"
    "🔁 *С интервалом:*\n"
    "— Напоминай каждые 2 дня в 20:00 откачать воду\n"
    "— Через день в 10:00 напоминай принять витамины\n"
    "— Раз в 3 дня в 9:00 полить цветы\n\n"
    "📝 *Заметки* (напомню каждый день в 9:00):\n"
    "— Запомни что нужно купить краску\n"
    "— Запиши: позвонить маме\n\n"
    "💪 *Привычки:*\n"
    "— Хочу каждый день делать зарядку\n"
    "— Каждый второй день медитация\n\n"
    "✨ *Цитаты* (каждый день в 8:00):\n"
    "— Присылай цитаты, пример: Успех — это привычка\n\n"
    "💬 *Просто поговори:*\n"
    "— Как дела? / Что думаешь о...?\n"
    "— Объясни мне / Посоветуй...\n\n"
    "📋 /list — список всех задач\n"
    "☀️ /today — план на сегодня\n"
    "✨ /quote — цитата прямо сейчас\n"
    "🗑 /delete [номер] — удалить задачу\n"
    "🔑 /myid — узнать свой Telegram ID\n"
    "📋 /menu — кнопки быстрого доступа\n"
)


# ────────────────────── ОБРАБОТЧИКИ ───────────────────────────
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
    await u.effective_message.reply_text(
        "Выбери действие:", reply_markup=main_keyboard()
    )

async def cmd_myid(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает Telegram ID — доступно всем."""
    user = u.effective_user
    await u.effective_message.reply_text(
        f"🔑 *Твой Telegram ID:*\n`{user.id}`\n\n"
        f"Имя: {user.first_name or '—'}\n"
        f"Username: @{user.username or '—'}",
        parse_mode="Markdown"
    )

async def cmd_list(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = db_get(cid)
    if not items:
        await u.effective_message.reply_text("Задач пока нет. Напиши мне что-нибудь! 😊")
        return

    groups = {
        "reminder":     ("⏰ Напоминания",  []),
        "habit":        ("💪 Привычки",     []),
        "note":         ("📝 Заметки",      []),
        "quote_config": ("✨ Цитаты",       []),
    }
    for it in items:
        k = it.get("type", "reminder")
        if k in groups:
            groups[k][1].append(it)

    lines = ["📋 *Твои задачи:*\n"]
    for _, (label, grp) in groups.items():
        if not grp:
            continue
        lines.append(f"*{label}:*")
        for it in grp:
            h  = it.get("remind_hour")
            m  = it.get("remind_minute", 0)
            iv = int(it.get("interval_days") or 1)
            ts = f" _{h:02d}:{m:02d}_" if h is not None else ""
            if iv > 1:
                ds = f" _(каждые {iv} дн.)_"
            elif it.get("is_daily"):
                ds = " _(ежедн.)_"
            else:
                ds = ""
            lines.append(f"  `{it['id']}` — {it['text']}{ts}{ds}")
        lines.append("")
    lines.append("_Удалить: /delete [номер]_")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_today(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = db_get(cid)
    if not items:
        await u.effective_message.reply_text("Задач нет. Отличный день! ☀️")
        return

    now   = datetime.now(TZ)
    emoji = {"reminder": "⏰", "habit": "💪", "note": "📝", "quote_config": "✨"}
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
            lines.append(f"  {emoji.get(it['type'], '•')} {it['text']}")
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


# ─────────────── CALLBACK (нажатие inline-кнопок) ─────────────
async def handle_callback(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query
    await query.answer()

    if query.data == "myid":
        await cmd_myid(u, ctx)
        return

    if not await check_access(u): return

    dispatch = {
        "list":  cmd_list,
        "today": cmd_today,
        "quote": cmd_quote,
        "help":  cmd_help,
    }
    handler = dispatch.get(query.data)
    if handler:
        await handler(u, ctx)


# ──────────────────── ОБРАБОТЧИК СООБЩЕНИЙ ────────────────────
async def handle_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return

    cid  = u.effective_chat.id
    uid  = u.effective_user.id
    text = u.message.text

    await u.message.reply_chat_action("typing")

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
                "Пример: _Напомни в 18:00 почистить зубы_",
                parse_mode="Markdown"
            )
            return

        db_add(cid, "reminder", content, int(hour), minute, daily, interval_days)

        if interval_days > 1:
            freq = f"каждые {interval_days} дн."
        elif daily:
            freq = "каждый день"
        else:
            freq = "один раз"

        await u.message.reply_text(
            f"✅ Запомнил!\n"
            f"⏰ *{int(hour):02d}:{minute:02d}* — {freq}\n"
            f"📌 _{content}_",
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

        if interval_days > 1:
            freq = f"каждые {interval_days} дн."
        else:
            freq = "каждый день"

        await u.message.reply_text(
            f"💪 Привычка добавлена!\n"
            f"⏰ *{h:02d}:{minute:02d}* — {freq}\n"
            f"📌 _{content}_",
            parse_mode="Markdown"
        )

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
                "Укажи номер задачи, например: _удали номер 5_\nСписок: /list",
                parse_mode="Markdown"
            )

    # ── Цитаты ───────────────────────────────────────────────────
    elif action == "set_quote":
        example = (intent.get("quote_example") or content
                   or "Каждый день — новый шанс")
        for old in db_get(cid, "quote_config"):
            db_off(old["id"], cid)
        db_add(cid, "quote_config", example, 8, 0, True)
        await u.message.reply_text(
            f"✨ Буду присылать цитаты каждый день в *8:00*!\n"
            f"Стиль: _{example}_",
            parse_mode="Markdown"
        )

    # ── Чат / всё остальное ──────────────────────────────────────
    else:
        reply = await ai_chat_reply(uid, text)
        await u.message.reply_text(reply)


# ──────────────── РЕГИСТРАЦИЯ КОМАНД В TELEGRAM ───────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "👋 Начало работы"),
        BotCommand("menu",   "📋 Быстрое меню с кнопками"),
        BotCommand("list",   "📋 Все мои задачи"),
        BotCommand("today",  "☀️ План на сегодня"),
        BotCommand("quote",  "✨ Получить цитату прямо сейчас"),
        BotCommand("delete", "🗑 Удалить задачу по номеру"),
        BotCommand("myid",   "🔑 Узнать мой Telegram ID"),
        BotCommand("help",   "📖 Помощь и примеры"),
    ])
    log.info("✅ Bot commands registered")


# ──────────────────────────── MAIN ────────────────────────────
async def main():
    threading.Thread(target=start_flask, daemon=True).start()
    log.info(f"Flask started on :{PORT}")

    if ALLOWED_USERS:
        log.info(f"🔒 Access restricted to IDs: {ALLOWED_USERS}")
    else:
        log.info("🌐 Access open to all users (ALLOWED_USERS not set)")

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
    log.info("✅ Bot is running!")

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

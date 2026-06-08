# =============================================================
#  Telegram Personal Assistant Bot — v2
#  AI: Google Gemini 1.5 Flash (бесплатно, 1500 запросов/день)
#  DB: Supabase (PostgreSQL, бесплатно)
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
# В .env добавь: ALLOWED_USERS=123456789,987654321
# Если переменная пуста — бот открыт для всех
_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = set(
    int(x.strip()) for x in _raw.split(",") if x.strip().isdigit()
)

def is_allowed(user_id: int) -> bool:
    """True если список пуст (открытый бот) или пользователь в списке."""
    return not ALLOWED_USERS or user_id in ALLOWED_USERS

async def check_access(update: Update) -> bool:
    """Проверяет доступ. При отказе — отвечает с ID и возвращает False."""
    user = update.effective_user
    if is_allowed(user.id):
        return True
    name = user.first_name or "Пользователь"
    await update.effective_message.reply_text(
        f"⛔ *Доступ закрыт*\n\n"
        f"Привет, {name}! Этот бот личный.\n\n"
        f"Твой Telegram ID:\n`{user.id}`\n\n"
        f"Отправь его владельцу бота — он добавит тебя в список разрешённых.",
        parse_mode="Markdown"
    )
    log.info(
        f"BLOCKED  id={user.id}  name={user.first_name}  @{user.username}"
    )
    return False

# ──────────────────────── КЛИЕНТЫ ─────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
ai    = genai.GenerativeModel("gemini-1.5-flash")
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
# Выполни в Supabase SQL Editor один раз (добавляет новые столбцы):
#
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

# ─────────────────── ИИ (Gemini 1.5 Flash) ────────────────────
PARSE_PROMPT = """Ты — парсер команд личного Telegram-бота. Твоя задача — понять что хочет пользователь, даже если фраза длинная или написана в произвольной форме.
Сейчас: {now}

Пользователь написал: "{msg}"

Верни ТОЛЬКО JSON без markdown, без пояснений.

{{
  "action":        "add_reminder" | "add_note" | "add_habit" | "list" | "delete" | "set_quote" | "chat" | "unknown",
  "text":          "суть задачи одной короткой фразой",
  "hour":          null или 0-23,
  "minute":        0-59,
  "is_daily":      true или false,
  "interval_days": 1,
  "delete_id":     null или число,
  "quote_example": null или строка,
  "reply":         null или "ответ если action=chat"
}}

══════════ ПРАВИЛА (приоритет сверху вниз) ══════════

① Есть ВРЕМЯ (в XX:00 / в XX часов) + интервал (каждый день/второй/N дней):
   → add_reminder, hour=то что указано, is_daily=true
   → interval_days по интервалу

② "запомни/запиши/напомни что" + есть время или интервал:
   → это НЕ add_note! Это add_reminder с нужным временем и интервалом.
   → add_note только если нет ни времени ни интервала

③ Интервалы:
   каждый день / ежедневно          → interval_days=1
   каждый второй день / через день  → interval_days=2
   каждые N дней / раз в N дней     → interval_days=N

④ Ключевые слова без времени:
   "хочу каждый день X / привычка / напоминай каждый день" → add_habit, hour=8
   "запомни/запиши" без интервала → add_note, hour=9
   "список/покажи/что у меня" → list
   "удали/убери номер X" → delete, delete_id=X
   "цитаты/мотивация/вдохновение" → set_quote
   приветствие/разговор → chat, reply="ответ"

Если время не указано: add_reminder → hour=8, add_note → hour=9, add_habit → hour=8

══════════ КОНКРЕТНЫЕ ПРИМЕРЫ (обязательно учи!) ══════════

ВХОД:  "запомни: нужно напоминать мне каждый второй день, например 9 июня, 11 июня, в 20:00 откачать воду"
ВЫХОД: {{"action":"add_reminder","text":"Откачать воду","hour":20,"minute":0,"is_daily":true,"interval_days":2,"delete_id":null,"quote_example":null,"reply":null}}

ВХОД:  "каждый второй день в 20:00 напоминай откачать воду"
ВЫХОД: {{"action":"add_reminder","text":"Откачать воду","hour":20,"minute":0,"is_daily":true,"interval_days":2,"delete_id":null,"quote_example":null,"reply":null}}

ВХОД:  "напомни каждые 3 дня в 10:00 проверить почту"
ВЫХОД: {{"action":"add_reminder","text":"Проверить почту","hour":10,"minute":0,"is_daily":true,"interval_days":3,"delete_id":null,"quote_example":null,"reply":null}}

ВХОД:  "хочу каждый день делать зарядку"
ВЫХОД: {{"action":"add_habit","text":"Делать зарядку","hour":8,"minute":0,"is_daily":true,"interval_days":1,"delete_id":null,"quote_example":null,"reply":null}}

ВХОД:  "раз в два дня напоминай полить цветы"
ВЫХОД: {{"action":"add_habit","text":"Полить цветы","hour":8,"minute":0,"is_daily":true,"interval_days":2,"delete_id":null,"quote_example":null,"reply":null}}

ВХОД:  "запомни купить молоко"
ВЫХОД: {{"action":"add_note","text":"Купить молоко","hour":9,"minute":0,"is_daily":true,"interval_days":1,"delete_id":null,"quote_example":null,"reply":null}}

ВХОД:  "привет как дела"
ВЫХОД: {{"action":"chat","text":"","hour":null,"minute":0,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null,"reply":"Привет! Всё хорошо, готов помочь 😊"}}

ВАЖНО: interval_days всегда ≥ 1. Игнорируй примерные даты типа "например 9 июня, 11 июня" — это просто пояснение пользователя."""

async def ai_parse(text: str) -> dict:
    now    = datetime.now(TZ).strftime("%H:%M %d.%m.%Y")
    prompt = PARSE_PROMPT.format(now=now, msg=text)
    try:
        resp = await asyncio.to_thread(ai.generate_content, prompt)
        raw  = re.sub(r"```(?:json)?\s*|\s*```", "", resp.text).strip()
        data = json.loads(raw)
        # Защита: если action неизвестен но есть время — скорее всего это reminder
        if data.get("action") == "unknown" and data.get("hour") is not None:
            data["action"] = "add_reminder"
            data.setdefault("is_daily", False)
        return data
    except Exception as e:
        log.error(f"ai_parse error: {e}  |  raw: {resp.text[:200] if 'resp' in dir() else '—'}")
        return {"action": "unknown"}

async def ai_fallback_reply(text: str) -> str:
    """Умный ответ когда не удалось распознать команду."""
    prompt = (
        f"Ты — дружелюбный помощник в Telegram. Пользователь написал: '{text}'.\n"
        "Если это похоже на попытку добавить напоминание/привычку/заметку, но написано непонятно — "
        "объясни как именно нужно написать (коротко, 1-2 примера). "
        "Если это просто разговор — ответь по-человечески. "
        "Отвечай на русском, коротко (2-3 строки максимум)."
    )
    try:
        resp = await asyncio.to_thread(ai.generate_content, prompt)
        return resp.text.strip()
    except Exception:
        return "Попробуй написать проще, например: _Напомни в 20:00 откачать воду каждый второй день_"

async def ai_quote(example: str) -> str:
    prompt = (
        f'Создай вдохновляющую цитату на русском языке в стиле: "{example}". '
        "Верни ТОЛЬКО текст цитаты, без автора и кавычек."
    )
    try:
        resp = await asyncio.to_thread(ai.generate_content, prompt)
        return resp.text.strip().strip('"').strip("'")
    except Exception as e:
        log.error(f"ai_quote: {e}")
        return "Каждый день — это новый шанс стать лучше!"

# ──────────────────────── ПЛАНИРОВЩИК ─────────────────────────
async def tick(bot):
    """Каждую минуту — рассылает запланированное, учитывает interval_days."""
    now     = datetime.now(TZ)
    today_s = now.strftime("%Y-%m-%d")
    today_d = now.date()
    items   = db_due(now.hour, now.minute)

    for item in items:
        interval  = int(item.get("interval_days") or 1)
        last_sent = item.get("last_sent_date")

        # Проверяем интервал для задач "каждые N дней"
        if last_sent and interval > 1:
            try:
                last_d     = datetime.strptime(str(last_sent)[:10], "%Y-%m-%d").date()
                days_since = (today_d - last_d).days
                if days_since < interval:
                    continue   # ещё рано
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
            InlineKeyboardButton("📋 Мои задачи",  callback_data="list"),
            InlineKeyboardButton("☀️ На сегодня",  callback_data="today"),
        ],
        [
            InlineKeyboardButton("✨ Цитата",       callback_data="quote"),
            InlineKeyboardButton("📖 Помощь",       callback_data="help"),
        ],
        [
            InlineKeyboardButton("🔑 Мой Telegram ID", callback_data="myid"),
        ],
    ])

# ────────────────────── ТЕКСТЫ ─────────────────────────────────
WELCOME = (
    "👋 Привет! Я твой личный ассистент.\n\n"
    "Умею:\n"
    "⏰ Напоминать в нужное время\n"
    "📝 Хранить заметки и напоминать каждый день\n"
    "💪 Следить за привычками (каждый день или через день)\n"
    "✨ Присылать вдохновляющие цитаты\n\n"
    "*Пиши естественным языком:*\n"
    "• _Напомни в 18:00 почистить зубы_\n"
    "• _Запомни что нужно купить краску_\n"
    "• _Каждый второй день напоминай пить витаминку_\n"
    "• _Раз в три дня напоминай полить цветы_\n"
)

HELP_TEXT = (
    "📖 *Как пользоваться:*\n\n"
    "⏰ *Напоминания:*\n"
    "— Напомни в 18 часов почистить зубы\n"
    "— Каждый день в 7:00 напоминай о пробежке\n\n"
    "📝 *Заметки* (напомню каждый день в 9:00):\n"
    "— Запомни что нужно купить краску\n"
    "— Запиши: позвонить маме\n\n"
    "💪 *Привычки* (любой ритм):\n"
    "— Хочу каждый день делать зарядку\n"
    "— Каждый второй день напоминай пить витаминку\n"
    "— Раз в три дня напоминай полить цветы\n\n"
    "✨ *Цитаты* (каждый день в 8:00):\n"
    "— Присылай цитаты, пример: Успех — это привычка\n\n"
    "📋 /list — список всех задач\n"
    "☀️ /today — план на сегодня\n"
    "✨ /quote — цитата прямо сейчас\n"
    "🗑 /delete [номер] — удалить задачу\n"
    "🔑 /myid — узнать свой Telegram ID\n"
    "📋 /menu — кнопки быстрого доступа"
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
    """Показывает Telegram ID — доступно всем, чтобы владелец мог добавить."""
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

# ─────────────── CALLBACK (нажатие inline-кнопок) ─────────────
async def handle_callback(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query
    await query.answer()          # убирает "часики" у кнопки

    if query.data == "myid":
        await cmd_myid(u, ctx)    # myid не требует проверки доступа
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
    cid    = u.effective_chat.id
    text   = u.message.text
    await u.message.reply_chat_action("typing")

    intent        = await ai_parse(text)
    action        = intent.get("action", "unknown")
    content       = intent.get("text") or text
    hour          = intent.get("hour")
    minute        = int(intent.get("minute") or 0)
    daily         = bool(intent.get("is_daily", False))
    interval_days = int(intent.get("interval_days") or 1)

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
            freq = f" (каждые {interval_days} дн.)"
        elif daily:
            freq = " (каждый день)"
        else:
            freq = ""
        await u.message.reply_text(
            f"✅ Напомню в *{int(hour):02d}:{minute:02d}*{freq}:\n_{content}_",
            parse_mode="Markdown"
        )

    elif action == "add_note":
        db_add(cid, "note", content, 9, 0, True)
        await u.message.reply_text(
            f"📝 Записал! Напомню каждый день в *9:00*:\n_{content}_",
            parse_mode="Markdown"
        )

    elif action == "add_habit":
        db_add(cid, "habit", content, int(hour or 8), minute, True, interval_days)
        if interval_days > 1:
            freq = f"каждые {interval_days} дня/дней"
        else:
            freq = "каждый день"
        await u.message.reply_text(
            f"💪 Привычка добавлена! {freq.capitalize()} в "
            f"*{int(hour or 8):02d}:{minute:02d}*:\n_{content}_",
            parse_mode="Markdown"
        )

    elif action == "list":
        await cmd_list(u, ctx)

    elif action == "delete":
        did = intent.get("delete_id")
        if did:
            db_off(int(did), cid)
            await u.message.reply_text(f"✅ Задача #{did} удалена!")
        else:
            await u.message.reply_text("Укажи номер задачи.\nСписок: /list")

    elif action == "set_quote":
        example = (
            intent.get("quote_example") or content
            or "Каждый день — новый шанс"
        )
        for old in db_get(cid, "quote_config"):
            db_off(old["id"], cid)
        db_add(cid, "quote_config", example, 8, 0, True)
        await u.message.reply_text(
            f"✨ Буду присылать цитаты каждый день в *8:00*!\n"
            f"Стиль: _{example}_",
            parse_mode="Markdown"
        )

    elif action == "chat":
        reply = intent.get("reply") or "😊 Чем могу помочь?"
        await u.message.reply_text(reply)

    else:
        # Умный fallback — AI отвечает сам, объясняет как написать правильно
        reply = await ai_fallback_reply(text)
        await u.message.reply_text(
            reply,
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

# ──────────────── РЕГИСТРАЦИЯ КОМАНД В TELEGRAM ───────────────
async def post_init(app: Application):
    """Заполняет меню '/' в Telegram (кнопка рядом с полем ввода)."""
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
    log.info("✅ Bot commands registered in Telegram menu")

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

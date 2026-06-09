# =============================================================
#  Telegram Personal Assistant Bot — v3
#  AI  : Google Gemini 2.0 Flash
#  DB  : Supabase (PostgreSQL)
#  Host: Render.com + UptimeRobot
# =============================================================
# SQL для Supabase (один раз в SQL Editor):
#   ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS interval_days  INT  DEFAULT 1;
#   ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS last_sent_date DATE;
# Типы "memory" и "task" не требуют изменений схемы.
# =============================================================

import os, asyncio, logging, threading, json, re, random
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
TIMEZONE       = os.environ.get("TIMEZONE", "Europe/Warsaw")   # Польша
TZ             = pytz.timezone(TIMEZONE)

# ─────────────── БЕЛЫЙ СПИСОК ПОЛЬЗОВАТЕЛЕЙ ───────────────────
_raw = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set = set(
    int(x.strip()) for x in _raw.split(",") if x.strip().isdigit()
)

def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS

async def check_access(update: Update) -> bool:
    user = update.effective_user
    if is_allowed(user.id):
        return True
    name = user.first_name or "Пользователь"
    await update.effective_message.reply_text(
        f"⛔ *Доступ закрыт*\n\nПривет, {name}! Этот бот личный.\n\n"
        f"Твой Telegram ID:\n`{user.id}`\n\n"
        f"Отправь его владельцу бота — он добавит тебя в список.",
        parse_mode="Markdown"
    )
    log.info(f"BLOCKED  id={user.id}  name={user.first_name}  @{user.username}")
    return False

# ──────────────────────── КЛИЕНТЫ ─────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
ai = genai.GenerativeModel("gemini-2.0-flash")   # Gemini 2.0 Flash
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
    try:
        r = db.table("bot_items").insert(row).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        log.error(f"db_add error: {e}")
        return None

def db_get(chat_id, kind=None):
    try:
        q = (db.table("bot_items").select("*")
               .eq("chat_id", str(chat_id))
               .eq("is_active", True))
        if kind:
            q = q.eq("type", kind)
        return q.order("id").execute().data or []
    except Exception as e:
        log.error(f"db_get error: {e}")
        return []

def db_off(item_id, chat_id):
    try:
        (db.table("bot_items")
           .update({"is_active": False})
           .eq("id", item_id)
           .eq("chat_id", str(chat_id))
           .execute())
    except Exception as e:
        log.error(f"db_off error: {e}")

def db_due(hour, minute):
    try:
        return (db.table("bot_items").select("*")
                  .eq("is_active", True)
                  .eq("remind_hour", hour)
                  .eq("remind_minute", minute)
                  .execute().data or [])
    except Exception as e:
        log.error(f"db_due error: {e}")
        return []

def db_mark_sent(item_id: int, sent_date: str):
    try:
        (db.table("bot_items")
           .update({"last_sent_date": sent_date})
           .eq("id", item_id)
           .execute())
    except Exception as e:
        log.error(f"db_mark_sent error: {e}")

# ─────────────── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ──────────────────────
def build_memory_context(chat_id: int) -> str:
    """Загружает память пользователя (макс. 10 записей, 600 символов).
    Используется как контекст для AI — не влияет на скорость ответов."""
    try:
        items = db_get(chat_id, "memory")
        if not items:
            return ""
        facts = [m["text"] for m in items[-10:]]
        ctx = "Что известно о пользователе:\n" + "\n".join(f"- {f}" for f in facts)
        return ctx[:600]
    except Exception:
        return ""

def get_pending_task(chat_id: int):
    """Возвращает задачу для inline-напоминания с вероятностью ~1/3.
    Вызывается при каждом ответе бота — пользователь видит напоминание в разговоре."""
    try:
        tasks = db_get(chat_id, "task")
        if not tasks or random.random() > 0.34:
            return None
        return random.choice(tasks)["text"]
    except Exception:
        return None

async def safe_send(message, text, parse_mode="Markdown", **kwargs):
    """Отправляет сообщение. Если упал Markdown — шлёт plain text."""
    try:
        await message.reply_text(text, parse_mode=parse_mode, **kwargs)
    except Exception:
        plain = re.sub(r"[*_`\[\]]", "", text)
        try:
            await message.reply_text(plain, **kwargs)
        except Exception as e:
            log.error(f"safe_send failed: {e}")

# ─────────────────── ИИ (Gemini 2.0 Flash) ────────────────────

PARSE_PROMPT = """Ты — парсер команд личного Telegram-бота. Точно определи намерение.
Сейчас: {now}

Пользователь написал: "{msg}"

Верни ТОЛЬКО JSON без markdown, без пояснений:
{{
  "action":        "add_reminder"|"add_task"|"add_note"|"add_habit"|"add_memory"|"make_plan"|"list"|"delete"|"set_quote"|"chat"|"unknown",
  "text":          "суть одной короткой фразой",
  "hour":          null или 0-23,
  "minute":        0-59,
  "is_daily":      true или false,
  "interval_days": 1,
  "delete_id":     null или число,
  "quote_example": null или строка,
  "plan_items":    null или "список дел через запятую",
  "reply":         null
}}

══════════ ПРАВИЛА (приоритет сверху вниз) ══════════

add_reminder  — есть конкретное ВРЕМЯ (в 12:04, в 18 часов, в 7:30)
add_task      — "нужно/надо/не забыть/запомни что нужно [сделать что-то]" БЕЗ конкретного времени; может быть дедлайн типа "на среду", "до пятницы"
add_note      — "запомни/запиши [факт или вещь]" — нет действия, просто хранение
add_habit     — "каждый день X / привычка / хочу каждый день делать X"
add_memory    — ЛИЧНАЯ ИНФОРМАЦИЯ или СТИЛЬ ОБЩЕНИЯ: имя, место учёбы/работы, курс, страна, как обращаться, стиль общения
make_plan     — "составь план", "распланируй день", "хочу сегодня сделать X Y Z", перечисление дел для плана на день
list          — "список/покажи/что у меня есть"
delete        — "удали/убери номер X"
set_quote     — "цитаты/мотивация/вдохновение"
chat          — приветствие, вопрос, разговор, обсуждение

Если время не указано: add_reminder → hour=8, add_note → hour=9, add_habit → hour=8
interval_days всегда ≥ 1

══════════ ПРИМЕРЫ ══════════

"напомни в 12:04 попить воды"
→ {{"action":"add_reminder","text":"Попить воды","hour":12,"minute":4,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null,"plan_items":null,"reply":null}}

"запомни что нужно мне сделать домашнее задание на среду"
→ {{"action":"add_task","text":"Домашнее задание (до среды)","hour":null,"minute":0,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null,"plan_items":null,"reply":null}}

"запомни что меня зовут Павел, обращайся ко мне по-дружески"
→ {{"action":"add_memory","text":"Имя: Павел. Стиль: по-дружески","hour":null,"minute":0,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null,"plan_items":null,"reply":null}}

"запомни что я учусь на третьем курсе в Польше"
→ {{"action":"add_memory","text":"Учится на 3 курсе в Польше","hour":null,"minute":0,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null,"plan_items":null,"reply":null}}

"хочу сегодня поотжиматься, погулять и сходить в технику"
→ {{"action":"make_plan","text":"","hour":null,"minute":0,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null,"plan_items":"отжимания, прогулка, поход в магазин техники","reply":null}}

"составь план: сходить в магазин, поучить материал, позвонить другу"
→ {{"action":"make_plan","text":"","hour":null,"minute":0,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null,"plan_items":"магазин, учёба, звонок другу","reply":null}}

"каждый второй день в 20:00 напоминай откачать воду"
→ {{"action":"add_reminder","text":"Откачать воду","hour":20,"minute":0,"is_daily":true,"interval_days":2,"delete_id":null,"quote_example":null,"plan_items":null,"reply":null}}

"привет как дела"
→ {{"action":"chat","text":"","hour":null,"minute":0,"is_daily":false,"interval_days":1,"delete_id":null,"quote_example":null,"plan_items":null,"reply":null}}"""


async def ai_parse(text: str) -> dict:
    now    = datetime.now(TZ).strftime("%H:%M %d.%m.%Y")
    prompt = PARSE_PROMPT.format(now=now, msg=text.replace('"', "'"))
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(ai.generate_content, prompt),
            timeout=12.0
        )
        raw  = re.sub(r"```(?:json)?\s*|\s*```", "", resp.text).strip()
        data = json.loads(raw)
        # Страховка: если action=unknown, но есть час — скорее всего reminder
        if data.get("action") == "unknown" and data.get("hour") is not None:
            data["action"] = "add_reminder"
            data.setdefault("is_daily", False)
        return data
    except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as e:
        log.error(f"ai_parse error: {e}")
        return {"action": "unknown"}


async def ai_chat(text: str, context: str) -> str:
    """Умный ответ с учётом памяти пользователя."""
    now = datetime.now(TZ).strftime("%H:%M %d.%m.%Y")
    parts = [
        f"Ты — личный ассистент в Telegram. Сейчас: {now}.",
        "Отвечай кратко, живо, по-человечески, на русском. Без лишних слов.",
    ]
    if context:
        parts.append(context)
    parts.append(f"\nПользователь: {text}")
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(ai.generate_content, "\n".join(parts)),
            timeout=15.0
        )
        return (resp.text or "").strip()
    except (asyncio.TimeoutError, Exception) as e:
        log.error(f"ai_chat error: {e}")
        return ""


async def ai_make_plan(items: str, context: str) -> str:
    """Генерирует структурированный план дня."""
    now = datetime.now(TZ).strftime("%H:%M %d.%m.%Y")
    parts = [f"Ты — личный ассистент. Сейчас: {now}."]
    if context:
        parts.append(context)
    parts.append(
        f"\nСоставь чёткий план дня из этих дел: {items}\n"
        "Раздели по частям дня (утро / день / вечер). "
        "Учти нагрузку и логичный порядок. "
        "Используй эмодзи. Только план, без предисловий."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(ai.generate_content, "\n".join(parts)),
            timeout=15.0
        )
        return (resp.text or "").strip()
    except (asyncio.TimeoutError, Exception) as e:
        log.error(f"ai_make_plan error: {e}")
        return ""


async def ai_fallback_reply(text: str, context: str) -> str:
    """Умный ответ когда действие не распознано."""
    parts = ["Ты — дружелюбный помощник в Telegram."]
    if context:
        parts.append(context)
    parts.append(
        f"\nПользователь написал: '{text}'.\n"
        "Если это похоже на добавление задачи/напоминания — объясни как написать правильно (1 пример). "
        "Если это просто разговор — ответь по-человечески. "
        "Кратко (2-3 строки), на русском."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(ai.generate_content, "\n".join(parts)),
            timeout=12.0
        )
        return (resp.text or "").strip()
    except (asyncio.TimeoutError, Exception) as e:
        log.error(f"ai_fallback error: {e}")
        return ""


async def ai_quote(example: str) -> str:
    prompt = (
        f'Создай вдохновляющую цитату на русском в стиле: "{example}". '
        "Только текст цитаты, без автора и кавычек."
    )
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(ai.generate_content, prompt),
            timeout=10.0
        )
        return (resp.text or "").strip().strip('"').strip("'") or "Каждый день — шанс стать лучше!"
    except (asyncio.TimeoutError, Exception) as e:
        log.error(f"ai_quote error: {e}")
        return "Каждый день — новый шанс стать лучше!"


# ──────────────────────── ПЛАНИРОВЩИК ─────────────────────────
async def tick(bot):
    """Каждую минуту рассылает запланированные напоминания (❗ в начале)."""
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
                await bot.send_message(cid, f"❗📝 Не забудь: {item['text']}")
                db_mark_sent(item["id"], today_s)

            elif t == "habit":
                await bot.send_message(cid, f"❗💪 Время привычки: {item['text']}")
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
            InlineKeyboardButton("📋 Мои задачи",     callback_data="list"),
            InlineKeyboardButton("☀️ На сегодня",     callback_data="today"),
        ],
        [
            InlineKeyboardButton("🧠 Память",          callback_data="memory"),
            InlineKeyboardButton("✨ Цитата",           callback_data="quote"),
        ],
        [
            InlineKeyboardButton("📖 Помощь",           callback_data="help"),
            InlineKeyboardButton("🔑 Мой ID",           callback_data="myid"),
        ],
    ])

# ────────────────────── ТЕКСТЫ ─────────────────────────────────
WELCOME = (
    "👋 Привет! Я твой личный ассистент.\n\n"
    "Умею:\n"
    "❗ Напоминать в нужное время\n"
    "📌 Хранить задачи и напоминать в разговоре\n"
    "🧠 Запоминать информацию о тебе\n"
    "📅 Составлять план на день\n"
    "💪 Следить за привычками\n"
    "✨ Присылать вдохновляющие цитаты\n\n"
    "*Пиши естественным языком:*\n"
    "• _Напомни в 12:04 попить воды_\n"
    "• _Запомни что нужно сдать работу до пятницы_\n"
    "• _Запомни что меня зовут Паша_\n"
    "• _Хочу сегодня поотжиматься, погулять и сходить в технику_\n"
    "• _Каждый день в 7:00 напоминай о пробежке_\n"
)

HELP_TEXT = (
    "📖 *Как пользоваться:*\n\n"
    "❗ *Напоминания по времени:*\n"
    "— Напомни в 12:04 попить воды\n"
    "— Каждый день в 7:00 напоминай о пробежке\n"
    "— Каждые 2 дня в 20:00 напоминай полить цветы\n\n"
    "📌 *Задачи (напомню в разговоре ~раз в день):*\n"
    "— Запомни что нужно сдать работу до пятницы\n"
    "— Надо купить продукты\n\n"
    "🧠 *Память (контекст для общения):*\n"
    "— Запомни что меня зовут Паша\n"
    "— Обращайся ко мне по-дружески\n"
    "— Запомни что я учусь на третьем курсе в Польше\n"
    "— Общайся со мной без воды\n\n"
    "📅 *План на день:*\n"
    "— Хочу сегодня поотжиматься, погулять и сходить в технику\n"
    "— Составь план: учёба, спорт, магазин\n\n"
    "💪 *Привычки:*\n"
    "— Хочу каждый день делать зарядку\n"
    "— Каждый второй день напоминай пить витаминки\n\n"
    "📋 /list   — все задачи\n"
    "🧠 /memory — что я о тебе помню\n"
    "☀️ /today  — план на сегодня\n"
    "✨ /quote  — цитата прямо сейчас\n"
    "🗑 /delete [номер] — удалить запись\n"
    "🔑 /myid  — Telegram ID\n"
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
    user = u.effective_user
    await u.effective_message.reply_text(
        f"🔑 *Твой Telegram ID:*\n`{user.id}`\n\n"
        f"Имя: {user.first_name or '—'}\n"
        f"Username: @{user.username or '—'}",
        parse_mode="Markdown"
    )

async def cmd_memory(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает всё что бот запомнил о пользователе."""
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = db_get(cid, "memory")
    if not items:
        await u.effective_message.reply_text(
            "🧠 Память пуста.\n\nДобавь: _Запомни что меня зовут Паша_",
            parse_mode="Markdown"
        )
        return
    lines = ["🧠 *Что я о тебе знаю:*\n"]
    for it in items:
        lines.append(f"  `{it['id']}` — {it['text']}")
    lines.append("\n_Удалить запись: /delete [номер]_")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_list(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid   = u.effective_chat.id
    # Исключаем memory из общего списка (для неё есть /memory)
    items = [i for i in db_get(cid) if i.get("type") != "memory"]
    if not items:
        await u.effective_message.reply_text("Задач пока нет. Напиши мне что-нибудь! 😊")
        return

    groups = {
        "reminder":     ("❗ Напоминания",   []),
        "task":         ("📌 Задачи",        []),
        "habit":        ("💪 Привычки",      []),
        "note":         ("📝 Заметки",       []),
        "quote_config": ("✨ Цитаты",        []),
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
    items = [i for i in db_get(cid) if i.get("type") not in ("memory",)]
    if not items:
        await u.effective_message.reply_text("Задач нет. Отличный день! ☀️")
        return

    now   = datetime.now(TZ)
    emojis = {"reminder": "❗", "habit": "💪", "note": "📝", "task": "📌", "quote_config": "✨"}
    timed = sorted(
        [i for i in items if i.get("remind_hour") is not None],
        key=lambda x: (x["remind_hour"], x.get("remind_minute", 0))
    )
    untimed = [i for i in items if i.get("remind_hour") is None]

    lines = [f"☀️ *План на {now.strftime('%d.%m.%Y')}:*\n"]
    for it in timed:
        h, m = it["remind_hour"], it.get("remind_minute", 0)
        iv   = int(it.get("interval_days") or 1)
        e    = emojis.get(it["type"], "•")
        sfx  = f" _(каждые {iv} дн.)_" if iv > 1 else ""
        lines.append(f"{e} *{h:02d}:{m:02d}* — {it['text']}{sfx}")
    if untimed:
        lines.append("\n📌 *Задачи без времени:*")
        for it in untimed:
            lines.append(f"  {emojis.get(it['type'], '•')} {it['text']}")
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
    await u.effective_message.reply_text(f"✅ Запись #{args[0]} удалена!")

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
        "list":   cmd_list,
        "today":  cmd_today,
        "memory": cmd_memory,
        "quote":  cmd_quote,
        "help":   cmd_help,
    }
    handler = dispatch.get(query.data)
    if handler:
        await handler(u, ctx)

# ──────────────────── ОБРАБОТЧИК СООБЩЕНИЙ ────────────────────
async def handle_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid  = u.effective_chat.id
    text = u.message.text
    await u.message.reply_chat_action("typing")

    # Загружаем память один раз — используем для всех AI вызовов
    context = build_memory_context(cid)

    intent        = await ai_parse(text)
    action        = intent.get("action", "unknown")
    content       = intent.get("text") or text
    hour          = intent.get("hour")
    minute        = int(intent.get("minute") or 0)
    daily         = bool(intent.get("is_daily", False))
    interval_days = int(intent.get("interval_days") or 1)

    # ── Напоминание по времени ──────────────────────────────────
    if action == "add_reminder":
        if hour is None:
            await u.message.reply_text(
                "❓ В какое время напомнить?\n"
                "Пример: _Напомни в 12:04 попить воды_",
                parse_mode="Markdown"
            )
            return
        db_add(cid, "reminder", content, int(hour), minute, daily, interval_days)
        freq = (f" (каждые {interval_days} дн.)" if interval_days > 1
                else (" (каждый день)" if daily else ""))
        await u.message.reply_text(
            f"✅ Напомню в *{int(hour):02d}:{minute:02d}*{freq}:\n_{content}_",
            parse_mode="Markdown"
        )

    # ── Задача с inline-напоминанием ────────────────────────────
    elif action == "add_task":
        db_add(cid, "task", content)
        await u.message.reply_text(
            f"📌 Записал задачу:\n_{content}_\n\n"
            f"Буду напоминать в ходе общения 👌",
            parse_mode="Markdown"
        )

    # ── Заметка ─────────────────────────────────────────────────
    elif action == "add_note":
        db_add(cid, "note", content, 9, 0, True)
        await u.message.reply_text(
            f"📝 Записал! Напомню каждый день в *9:00*:\n_{content}_",
            parse_mode="Markdown"
        )

    # ── Привычка ────────────────────────────────────────────────
    elif action == "add_habit":
        db_add(cid, "habit", content, int(hour or 8), minute, True, interval_days)
        freq = f"каждые {interval_days} дн." if interval_days > 1 else "каждый день"
        await u.message.reply_text(
            f"💪 Привычка добавлена! {freq.capitalize()} в "
            f"*{int(hour or 8):02d}:{minute:02d}*:\n_{content}_",
            parse_mode="Markdown"
        )

    # ── Память ──────────────────────────────────────────────────
    elif action == "add_memory":
        db_add(cid, "memory", content)
        await u.message.reply_text(
            f"🧠 Запомнил:\n_{content}_\n\n"
            f"Буду учитывать это в общении 👌",
            parse_mode="Markdown"
        )

    # ── План на день ────────────────────────────────────────────
    elif action == "make_plan":
        items_text = intent.get("plan_items") or content or text
        plan = await ai_make_plan(items_text, context)
        if plan:
            await safe_send(u.message, f"📅 *План на день:*\n\n{plan}")
        else:
            await u.message.reply_text(
                "😕 Не смог составить план прямо сейчас. Попробуй ещё раз."
            )

    # ── Список ──────────────────────────────────────────────────
    elif action == "list":
        await cmd_list(u, ctx)

    # ── Удаление ────────────────────────────────────────────────
    elif action == "delete":
        did = intent.get("delete_id")
        if did:
            db_off(int(did), cid)
            await u.message.reply_text(f"✅ Запись #{did} удалена!")
        else:
            await u.message.reply_text("Укажи номер записи.\nСписок: /list")

    # ── Цитата ──────────────────────────────────────────────────
    elif action == "set_quote":
        example = intent.get("quote_example") or content or "Каждый день — новый шанс"
        for old in db_get(cid, "quote_config"):
            db_off(old["id"], cid)
        db_add(cid, "quote_config", example, 8, 0, True)
        await u.message.reply_text(
            f"✨ Буду присылать цитаты каждый день в *8:00*!\n"
            f"Стиль: _{example}_",
            parse_mode="Markdown"
        )

    # ── Разговор ────────────────────────────────────────────────
    elif action == "chat":
        reply = await ai_chat(text, context)
        if not reply:
            reply = "😊 Чем могу помочь?"
        # Inline-напоминание о задаче (с вероятностью ~1/3)
        task_hint = get_pending_task(cid)
        if task_hint:
            reply += f"\n\n📌 *Кстати, не забудь:* _{task_hint}_"
        await safe_send(u.message, reply)

    # ── Fallback ─────────────────────────────────────────────────
    else:
        reply = await ai_fallback_reply(text, context)
        if not reply:
            reply = "🤔 Не совсем понял. Попробуй написать иначе или посмотри /help"
        # Inline-напоминание о задаче (с вероятностью ~1/3)
        task_hint = get_pending_task(cid)
        if task_hint:
            reply += f"\n\n📌 *Кстати, не забудь:* _{task_hint}_"
        await safe_send(u.message, reply, reply_markup=main_keyboard())


# ──────────────── РЕГИСТРАЦИЯ КОМАНД В TELEGRAM ───────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "👋 Начало работы"),
        BotCommand("menu",   "📋 Быстрое меню"),
        BotCommand("list",   "📋 Все задачи"),
        BotCommand("memory", "🧠 Что я о тебе знаю"),
        BotCommand("today",  "☀️ План на сегодня"),
        BotCommand("quote",  "✨ Цитата прямо сейчас"),
        BotCommand("delete", "🗑 Удалить запись по номеру"),
        BotCommand("myid",   "🔑 Мой Telegram ID"),
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
    app.add_handler(CommandHandler("memory", cmd_memory))
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

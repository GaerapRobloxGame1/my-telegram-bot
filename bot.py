# =============================================================
#  Telegram Personal Assistant Bot
#  AI: Google Gemini 1.5 Flash (бесплатно, 1500 запросов/день)
#  DB: Supabase (PostgreSQL, бесплатно)
#  Host: Render.com + UptimeRobot
# =============================================================

import os, asyncio, logging, threading, json, re
from datetime import datetime
import pytz

from telegram import Update
from telegram.ext import (
    Application, CommandHandler,
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
def db_add(chat_id, kind, text, hour=None, minute=0, daily=False):
    row = {
        "chat_id": str(chat_id),
        "type": kind,
        "text": text,
        "remind_hour":   hour,
        "remind_minute": int(minute or 0),
        "is_daily":      bool(daily),
        "is_active":     True
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

# ─────────────────── ИИ (Gemini 1.5 Flash) ────────────────────
PARSE_PROMPT = """Ты — парсер команд Telegram-бота.
Сейчас: {now}

Верни ТОЛЬКО JSON без markdown и пояснений:

Сообщение: "{msg}"

{{
  "action":        "add_reminder"|"add_note"|"add_habit"|"list"|"delete"|"set_quote"|"unknown",
  "text":          "текст задачи",
  "hour":          null или 0-23,
  "minute":        0-59,
  "is_daily":      true/false,
  "delete_id":     null или число,
  "quote_example": null или "пример цитаты"
}}

Правила:
• "напомни в X" → add_reminder, hour=X
• "каждый день в X" → add_reminder, is_daily=true, hour=X
• "запомни/запиши/не забыть" → add_note, hour=9, is_daily=true
• "каждый день X / привычка / хочу ежедневно" → add_habit, hour=8, is_daily=true
• "покажи/список/что у меня" → list
• "удали/убери" → delete, delete_id=число из текста
• "цитата/мотивация/вдохновение" → set_quote
• Без указанного времени → hour=8
• Язык текста — сохранить как у пользователя"""

async def ai_parse(text: str) -> dict:
    now = datetime.now(TZ).strftime("%H:%M %d.%m.%Y")
    prompt = PARSE_PROMPT.format(now=now, msg=text)
    try:
        resp = await asyncio.to_thread(ai.generate_content, prompt)
        raw  = re.sub(r"```(?:json)?\s*|\s*```", "", resp.text).strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"ai_parse: {e}")
        return {"action": "unknown"}

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
    """Запускается каждую минуту — отправляет всё что запланировано."""
    now   = datetime.now(TZ)
    items = db_due(now.hour, now.minute)
    for item in items:
        cid = int(item["chat_id"])
        try:
            t = item["type"]
            if t == "reminder":
                await bot.send_message(cid, f"⏰ Напоминание: {item['text']}")
                if not item["is_daily"]:
                    db_off(item["id"], cid)          # разовое — удаляем

            elif t == "note":
                await bot.send_message(cid, f"📝 Не забудь: {item['text']}")

            elif t == "habit":
                await bot.send_message(cid, f"💪 Время привычки: {item['text']}")

            elif t == "quote_config":
                q = await ai_quote(item["text"])
                await bot.send_message(
                    cid, f"✨ *Цитата дня:*\n\n{q}", parse_mode="Markdown"
                )
        except Exception as e:
            log.error(f"tick item {item.get('id')}: {e}")

# ────────────────────── ОБРАБОТЧИКИ БОТА ──────────────────────
WELCOME = (
    "👋 Привет! Я твой личный ассистент.\n\n"
    "Умею:\n"
    "⏰ Напоминать в нужное время\n"
    "📝 Хранить заметки и напоминать каждый день\n"
    "💪 Следить за ежедневными привычками\n"
    "✨ Присылать вдохновляющие цитаты\n\n"
    "*Просто напиши мне:*\n"
    "• _Напомни в 18:00 почистить зубы_\n"
    "• _Запомни: нужно покрасить стену_\n"
    "• _Каждый день напоминай отжиматься 20 раз_\n"
    "• _Присылай цитаты, например: Каждый день — новый шанс_\n\n"
    "/list — мои задачи  |  /help — помощь  |  /today — план на день"
)

HELP = (
    "📖 *Как пользоваться:*\n\n"
    "⏰ *Напоминания:*\n"
    "— Напомни в 18 часов почистить зубы\n"
    "— Каждый день в 7:00 напоминай о пробежке\n\n"
    "📝 *Заметки* (напомню каждый день в 9:00):\n"
    "— Запомни что нужно купить краску\n"
    "— Запиши: позвонить маме\n\n"
    "💪 *Привычки* (каждый день в 8:00):\n"
    "— Хочу каждый день делать зарядку\n"
    "— Напоминай каждый день пить 2 литра воды\n\n"
    "✨ *Цитаты* (каждый день в 8:00):\n"
    "— Присылай цитаты, пример: Успех — это привычка\n\n"
    "📋 /list — список всех задач\n"
    "📅 /today — план на сегодня\n"
    "✨ /quote — цитата прямо сейчас\n"
    "🗑 /delete [номер] — удалить задачу"
)

async def cmd_start(u: Update, _):
    await u.message.reply_text(WELCOME, parse_mode="Markdown")

async def cmd_help(u: Update, _):
    await u.message.reply_text(HELP, parse_mode="Markdown")

async def cmd_list(u: Update, _):
    cid   = u.effective_chat.id
    items = db_get(cid)
    if not items:
        await u.message.reply_text("Задач пока нет. Напиши мне что-нибудь! 😊")
        return

    groups = {
        "reminder":    ("⏰ Напоминания",  []),
        "habit":       ("💪 Привычки",     []),
        "note":        ("📝 Заметки",      []),
        "quote_config":("✨ Цитаты",       []),
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
            h = it.get("remind_hour")
            m = it.get("remind_minute", 0)
            ts = f" _{h:02d}:{m:02d}_" if h is not None else ""
            ds = " _(ежедн.)_" if it.get("is_daily") else ""
            lines.append(f"  `{it['id']}` — {it['text']}{ts}{ds}")
        lines.append("")
    lines.append("_Удалить: /delete [номер]_")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_today(u: Update, _):
    cid   = u.effective_chat.id
    items = db_get(cid)
    if not items:
        await u.message.reply_text("Задач нет. Отличный день! ☀️")
        return

    now = datetime.now(TZ)
    emoji = {"reminder": "⏰", "habit": "💪", "note": "📝", "quote_config": "✨"}
    timed   = sorted(
        [i for i in items if i.get("remind_hour") is not None],
        key=lambda x: (x["remind_hour"], x.get("remind_minute", 0))
    )
    untimed = [i for i in items if i.get("remind_hour") is None]

    lines = [f"☀️ *План на {now.strftime('%d.%m.%Y')}:*\n"]
    for it in timed:
        h, m = it["remind_hour"], it.get("remind_minute", 0)
        e = emoji.get(it["type"], "•")
        lines.append(f"{e} *{h:02d}:{m:02d}* — {it['text']}")
    if untimed:
        lines.append("\n📌 *Без времени:*")
        for it in untimed:
            lines.append(f"  {emoji.get(it['type'],'•')} {it['text']}")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_delete(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid  = u.effective_chat.id
    args = ctx.args
    if not args or not args[0].isdigit():
        await u.message.reply_text(
            "Укажи номер: `/delete 5`\nСписок: /list", parse_mode="Markdown"
        )
        return
    db_off(int(args[0]), cid)
    await u.message.reply_text(f"✅ Задача #{args[0]} удалена!")

async def cmd_quote(u: Update, _):
    cid     = u.effective_chat.id
    configs = db_get(cid, "quote_config")
    example = configs[0]["text"] if configs else "Каждый день — шанс стать лучше"
    await u.message.reply_chat_action("typing")
    q = await ai_quote(example)
    await u.message.reply_text(f"✨ *Цитата:*\n\n{q}", parse_mode="Markdown")

async def handle_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid    = u.effective_chat.id
    text   = u.message.text
    await u.message.reply_chat_action("typing")

    intent = await ai_parse(text)
    action = intent.get("action", "unknown")

    if action == "add_reminder":
        hour    = intent.get("hour")
        minute  = int(intent.get("minute") or 0)
        content = intent.get("text") or text
        daily   = bool(intent.get("is_daily", False))
        if hour is None:
            await u.message.reply_text(
                "❓ В какое время напомнить?\n"
                "Пример: _Напомни в 18:00 почистить зубы_",
                parse_mode="Markdown"
            )
            return
        db_add(cid, "reminder", content, int(hour), minute, daily)
        d = " (каждый день)" if daily else ""
        await u.message.reply_text(
            f"✅ Напомню в *{int(hour):02d}:{minute:02d}*{d}:\n_{content}_",
            parse_mode="Markdown"
        )

    elif action == "add_note":
        content = intent.get("text") or text
        db_add(cid, "note", content, 9, 0, True)
        await u.message.reply_text(
            f"📝 Записал! Напомню каждый день в *9:00*:\n_{content}_",
            parse_mode="Markdown"
        )

    elif action == "add_habit":
        content = intent.get("text") or text
        hour    = int(intent.get("hour") or 8)
        minute  = int(intent.get("minute") or 0)
        db_add(cid, "habit", content, hour, minute, True)
        await u.message.reply_text(
            f"💪 Привычка добавлена! Каждый день в *{hour:02d}:{minute:02d}*:\n_{content}_",
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
            intent.get("quote_example") or intent.get("text")
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

    else:
        await u.message.reply_text(
            "🤔 Не понял. Попробуй:\n\n"
            "• _Напомни в 18:00 почистить зубы_\n"
            "• _Запомни: купить продукты_\n"
            "• _Каждый день напоминай о зарядке_\n\n"
            "Или напиши /help",
            parse_mode="Markdown"
        )

# ──────────────────────────── MAIN ────────────────────────────
async def main():
    # Keep-alive HTTP сервер в отдельном потоке
    threading.Thread(target=start_flask, daemon=True).start()
    log.info(f"Flask started on :{PORT}")

    # Telegram Application
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("today",  cmd_today))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("quote",  cmd_quote))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    # Планировщик — каждую минуту проверяет задачи
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
        await asyncio.Event().wait()   # висим вечно
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        sched.shutdown()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())

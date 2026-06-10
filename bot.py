# =============================================================
#  Telegram Personal Assistant Bot — v4.2
#  AI:  Gemini 3.5 Flash
#  DB:  Supabase
#
#  ПРЕФИКСНАЯ СИСТЕМА (пиши в любом формате):
#    (ПАМЯТЬ) запомни что меня зовут Паша
#    (НАПОМНИ) в 12:03 выпить воды
#    (ЗАМЕТКА) купить молоко
#    (ПРИВЫЧКА) каждый день в 7:00 зарядка
#    (ЗАДАЧА) сдать ДЗ до среды
#    (ПЛАН) отжаться, погулять, магазин
#    (ЦИТАТЫ) пример: Успех — это привычка
#
#  SQL — выполни ОДИН РАЗ в Supabase SQL Editor:
#  ──────────────────────────────────────────────
#  CREATE TABLE IF NOT EXISTS bot_memory (
#    chat_id TEXT PRIMARY KEY,
#    facts   TEXT DEFAULT '',
#    updated_at TIMESTAMPTZ DEFAULT NOW()
#  );
#  ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS interval_days   INT  DEFAULT 1;
#  ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS last_sent_date  TEXT;
#  ALTER TABLE bot_items ADD COLUMN IF NOT EXISTS last_reminded   TEXT;
#  ──────────────────────────────────────────────
# =============================================================

import os, asyncio, logging, threading, json, re
from datetime import datetime
import pytz

from telegram import (
    Update, BotCommand, BotCommandScopeAllPrivateChats,
    InlineKeyboardButton, InlineKeyboardMarkup,
    MenuButtonCommands,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from supabase import create_client, Client
from flask import Flask

# ─────────────────────── НАСТРОЙКИ ────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
PORT           = int(os.environ.get("PORT", 5000))
TIMEZONE       = os.environ.get("TIMEZONE", "Europe/Moscow")
TZ             = pytz.timezone(TIMEZONE)
MODEL          = "gemini-3.5-flash"

# ══════════════════════════════════════════════════════════════
#  SDK — google-genai (новый) или google-generativeai (старый)
# ══════════════════════════════════════════════════════════════
try:
    from google import genai as _g
    from google.genai import types as _t

    _ai = _g.Client(api_key=GEMINI_API_KEY)

    def _call(prompt: str, system: str = None,
              json_mode: bool = False, temp: float = 0.7) -> str:
        kw = {"temperature": temp}
        if system:
            kw["system_instruction"] = system
        if json_mode:
            kw["response_mime_type"] = "application/json"
        return _ai.models.generate_content(
            model=MODEL, contents=prompt,
            config=_t.GenerateContentConfig(**kw),
        ).text

    log.info(f"✅ SDK: google-genai | {MODEL}")

except ImportError:
    import google.generativeai as _g_old
    _g_old.configure(api_key=GEMINI_API_KEY)

    def _call(prompt: str, system: str = None,
              json_mode: bool = False, temp: float = 0.7) -> str:
        full = f"{system}\n\n{prompt}" if system else prompt
        try:
            cfg = _g_old.GenerationConfig(
                temperature=temp,
                **({"response_mime_type": "application/json"} if json_mode else {}),
            )
        except TypeError:
            cfg = _g_old.GenerationConfig(temperature=temp)
        return _g_old.GenerativeModel(MODEL, generation_config=cfg).generate_content(full).text

    log.info(f"⚠️  SDK: google-generativeai (старый) | {MODEL}")

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
        f"⛔ *Доступ закрыт*\n\nПривет, {user.first_name or 'Пользователь'}!\n"
        f"Твой ID:\n`{user.id}`",
        parse_mode="Markdown",
    )
    return False

# ──────────────────────── КЛИЕНТЫ ─────────────────────────────
db: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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
def db_add(chat_id, kind, text, hour=None, minute=0,
           daily=False, interval_days=1) -> dict | None:
    row = {
        "chat_id": str(chat_id), "type": kind, "text": text,
        "remind_hour": hour, "remind_minute": int(minute or 0),
        "is_daily": bool(daily), "is_active": True, "last_sent_date": None,
    }
    try:
        r = db.table("bot_items").insert({**row, "interval_days": int(interval_days or 1)}).execute()
        return r.data[0] if r.data else None
    except Exception:
        pass
    try:
        r = db.table("bot_items").insert(row).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        log.error(f"db_add: {e}"); return None

def db_get(chat_id, kind=None) -> list:
    try:
        q = (db.table("bot_items").select("*")
               .eq("chat_id", str(chat_id)).eq("is_active", True))
        if kind:
            q = q.eq("type", kind)
        return q.order("id").execute().data or []
    except Exception as e:
        log.error(f"db_get: {e}"); return []

def db_off(item_id, chat_id):
    try:
        (db.table("bot_items").update({"is_active": False})
           .eq("id", item_id).eq("chat_id", str(chat_id)).execute())
    except Exception as e:
        log.error(f"db_off: {e}")

def db_due(hour, minute) -> list:
    try:
        return (db.table("bot_items").select("*")
                  .eq("is_active", True).eq("remind_hour", hour)
                  .eq("remind_minute", minute).execute().data or [])
    except Exception as e:
        log.error(f"db_due: {e}"); return []

def db_mark_sent(item_id: int, sent_date: str):
    try:
        db.table("bot_items").update({"last_sent_date": sent_date}).eq("id", item_id).execute()
    except Exception as e:
        log.error(f"db_mark_sent: {e}")

# ── Память ──────────────────────────────────────────────────
MEM_MAX = 500

def mem_load(chat_id: int) -> str:
    try:
        r = db.table("bot_memory").select("facts").eq("chat_id", str(chat_id)).execute()
        return (r.data[0].get("facts") or "").strip() if r.data else ""
    except Exception:
        return ""

def mem_save(chat_id: int, facts: str):
    try:
        db.table("bot_memory").upsert({
            "chat_id": str(chat_id), "facts": facts[:MEM_MAX],
            "updated_at": datetime.now(TZ).isoformat(),
        }).execute()
    except Exception as e:
        log.error(f"mem_save: {e}")

def mem_add_fact(chat_id: int, fact: str):
    cur = mem_load(chat_id)
    if fact.lower().strip() in cur.lower():
        return
    updated = (cur + "\n" + fact).strip()
    if len(updated) > MEM_MAX:
        lines = updated.split("\n")
        while len("\n".join(lines)) > MEM_MAX and lines:
            lines.pop(0)
        updated = "\n".join(lines)
    mem_save(chat_id, updated)

def mem_clear(chat_id: int):
    mem_save(chat_id, "")

# ── Мягкие задачи (~каждые 8 ч) ────────────────────────────
def _hour_block(dt: datetime) -> str:
    b = 0 if dt.hour < 8 else (8 if dt.hour < 16 else 16)
    return f"{dt.strftime('%Y-%m-%d')}-{b}"

def soft_add(chat_id: int, text: str):
    db_add(chat_id, "soft_task", text)

def soft_pending(chat_id: int) -> list[str]:
    try:
        block = _hour_block(datetime.now(TZ))
        items = (db.table("bot_items").select("*")
                   .eq("chat_id", str(chat_id))
                   .eq("type", "soft_task").eq("is_active", True)
                   .execute().data or [])
        out = []
        for it in items:
            if (it.get("last_reminded") or "") != block:
                out.append(it["text"])
                try:
                    db.table("bot_items").update({"last_reminded": block}).eq("id", it["id"]).execute()
                except Exception:
                    pass
        return out
    except Exception as e:
        log.error(f"soft_pending: {e}"); return []

# ══════════════════════════════════════════════════════════════
#  ПРЕФИКСНАЯ СИСТЕМА
#  Форматы: (ПАМЯТЬ) текст   |   ПАМЯТЬ: текст
# ══════════════════════════════════════════════════════════════
PREFIX_MAP: dict[str, str] = {
    # Память
    "ПАМЯТЬ":       "add_memory",
    # Напоминания
    "НАПОМНИ":      "add_reminder",
    "НАПОМИНАНИЕ":  "add_reminder",
    "НАПОМИНАЙ":    "add_reminder",
    # Заметки
    "ЗАМЕТКА":      "add_note",
    "ЗАПИШИ":       "add_note",
    # Привычки
    "ПРИВЫЧКА":     "add_habit",
    "ПРИВЫЧКИ":     "add_habit",
    # Мягкие задачи
    "ЗАДАЧА":       "add_soft_task",
    "ДЕДЛАЙН":      "add_soft_task",
    # День план
    "ПЛАН":         "make_plan",
    # Цитаты
    "ЦИТАТЫ":       "set_quote",
    "ЦИТАТА":       "set_quote",
    # Служебные
    "УДАЛИ":        "delete",
    "СПИСОК":       "list",
}

# Описание для /help
PREFIX_HELP = {
    "ПАМЯТЬ":       "постоянные факты о тебе",
    "НАПОМНИ":      "напоминание в конкретное время",
    "ЗАМЕТКА":      "заметка (каждый день в 9:00)",
    "ПРИВЫЧКА":     "регулярная привычка",
    "ЗАДАЧА":       "задача с дедлайном (~8ч)",
    "ПЛАН":         "составить план на день",
    "ЦИТАТЫ":       "настроить ежедневные цитаты",
}

def parse_prefix(text: str) -> tuple[str | None, str]:
    """
    Определяет префикс в тексте.
    Форматы:
      (ПАМЯТЬ) запомни что...
      ПАМЯТЬ: запомни что...
    Возвращает (action, cleaned_text) или (None, original_text).
    """
    t = text.strip()
    # (КЛЮЧЕВОЕ_СЛОВО) текст
    if m := re.match(r'^\(([^)]+)\)\s*(.*)', t, re.I | re.S):
        key  = m.group(1).upper().strip()
        body = m.group(2).strip()
    # КЛЮЧЕВОЕ_СЛОВО: текст
    elif m := re.match(r'^([А-ЯЁа-яё]+):\s*(.*)', t, re.I | re.S):
        key  = m.group(1).upper().strip()
        body = m.group(2).strip()
    else:
        return None, text

    action = PREFIX_MAP.get(key)
    if not action:
        return None, text
    return action, body or text

# ══════════════════════════════════════════════════════════════
#  ПАРСЕР  (keyword-first → AI → regex fallback)
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


def _keyword_intent(text: str) -> str | None:
    t = text.lower()
    if re.search(r'\bнапомни\b|\bнапоминай\b', t):     return "add_reminder"
    if re.search(r'\bсоставь\s+план|\bпланируй\b|\bплан\s+на\s+день\b', t): return "make_plan"
    if re.search(r'\bсписок\b|покажи.*задач|что\s+у\s+меня\s+есть', t):     return "list"
    if re.search(r'\bудали\b|\bубери\b', t) and re.search(r'\d+', t):        return "delete"
    if re.search(r'\bзапомни\b|\bзапиши\b', t):
        if re.search(r'меня зовут|мой стиль|обращайся|я учусь|я работаю|'
                     r'я живу|общайся|говори мне|ко мне|моё имя', t):
            return "add_memory"
        if re.search(r'\bнужно\b|\bнадо\b|\bдолжен\b|'
                     r'на\s+(понедельник|вторник|среду|четверг|пятниц|суббот|'
                     r'воскресен|следующ)', t):
            return "add_soft_task"
        return "add_note"
    if re.search(r'не\s+забудь\s+напомнить|напомни\s+позже', t): return "add_soft_task"
    if re.search(r'моя\s+память|что\s+ты\s+помнишь|покажи\s+память', t):    return "show_memory"
    if re.search(r'забудь\s+всё|очисти\s+память|удали\s+память', t):         return "clear_memory"
    if re.search(r'присылай\s+цитат|настрой\s+цитат', t):                    return "set_quote"
    return None


def _extract_text(action: str, raw: str) -> str:
    t = raw
    if action in ("add_reminder", "add_habit"):
        for p in [r'напомни(те)?\s*(мне)?\s*', r'напоминай\s*(мне)?\s*',
                  r'каждые?\s+\d+\s+дн\w*', r'через\s+день',
                  r'каждый\s+(второй\s+)?день', r'ежедневно',
                  r'раз\s+в\s+\d+\s+дн\w*', r'в\s+\d{1,2}:\d{2}',
                  r'в\s+\d{1,2}\s+(часов|ч|утра|вечера)',
                  r'в\s+(полдень|полночь)']:
            t = re.sub(p, '', t, flags=re.I)
    elif action in ("add_note", "add_soft_task"):
        t = re.sub(r'^запомни\s*(что)?\s*', '', t, flags=re.I)
        t = re.sub(r'^запиши\s*(что)?\s*',  '', t, flags=re.I)
        t = re.sub(r'^не\s+забудь\s*(что)?\s*', '', t, flags=re.I)
    elif action == "add_memory":
        t = re.sub(r'^запомни\s*(что)?\s*', '', t, flags=re.I)
        t = re.sub(r'^запиши\s*(что)?\s*',  '', t, flags=re.I)
    return t.strip() or raw.strip()


PARSE_SYS = """\
Ты — точный парсер команд. Верни ТОЛЬКО валидный JSON.
Действия: add_reminder, add_note, add_habit, add_memory, add_soft_task,
          make_plan, show_memory, clear_memory, list, delete, set_quote, chat.
"""
PARSE_TMPL = """\
Время: {now}
Сообщение: "{msg}"
Подсказка действия: {hint}

JSON:
{{
  "action": "{hint}",
  "text": "суть кратко",
  "hour": null, "minute": 0,
  "is_daily": false, "interval_days": 1,
  "delete_id": null, "quote_example": null,
  "memory_fact": null, "plan_activities": null
}}

Время: "в 12:03"→h=12,m=3 | "в 20 часов"→h=20 | "в 8 вечера"→h=20
Интервал: "через день"→2 | "каждые 3 дня"→3 | "каждый день"→1
add_memory: memory_fact = факт кратко
make_plan:  plan_activities = список дел через запятую
"""

async def ai_parse(text: str) -> dict:
    hints   = _regex_hints(text)
    keyword = _keyword_intent(text)
    now     = datetime.now(TZ).strftime("%H:%M %d.%m.%Y")

    # Быстрый путь: keyword + время без AI
    if keyword == "add_reminder" and hints.get("hour") is not None:
        return {"action": "add_reminder", "text": _extract_text("add_reminder", text),
                "hour": hints["hour"], "minute": hints.get("minute", 0),
                "is_daily": hints.get("is_daily", False),
                "interval_days": hints.get("interval_days", 1)}
    if keyword in ("list", "show_memory", "clear_memory"):
        return {"action": keyword}
    if keyword == "delete":
        m = re.search(r'\d+', text)
        return {"action": "delete", "delete_id": int(m.group()) if m else None}

    prompt = PARSE_TMPL.format(now=now, msg=text, hint=keyword or "chat")
    try:
        raw  = await asyncio.to_thread(_call, prompt, PARSE_SYS, True, 0.05)
        data = json.loads(raw)
        if keyword and data.get("action") in ("chat", "unknown", None):
            data["action"] = keyword
        if hints.get("hour") is not None and data.get("hour") is None:
            if data.get("action") in ("add_reminder", "add_habit"):
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
        if keyword:
            return {"action": keyword, "text": _extract_text(keyword, text),
                    "hour": hints.get("hour"), "minute": hints.get("minute", 0),
                    "is_daily": hints.get("is_daily", False),
                    "interval_days": hints.get("interval_days", 1)}
        if hints.get("hour") is not None:
            return {"action": "add_reminder", "text": text.strip(),
                    "hour": hints["hour"], "minute": hints.get("minute", 0),
                    "is_daily": hints.get("is_daily", False),
                    "interval_days": hints.get("interval_days", 1)}
        return {"action": "chat"}

# ══════════════════════════════════════════════════════════════
#  AI ФУНКЦИИ
# ══════════════════════════════════════════════════════════════
async def ai_confirm(summary: str, memory: str = "") -> str | None:
    mem = f"\n[О пользователе: {memory}]" if memory else ""
    prompt = (f"Ты ассистент.{mem}\n"
              f"Только что выполнил: {summary}\n\n"
              f"Напиши ОДНО короткое (1–2 предложения) подтверждение на русском. "
              f"Дружелюбно, без лишних слов.")
    try:
        return (await asyncio.to_thread(_call, prompt, None, False, 0.7)).strip()
    except Exception as e:
        log.error(f"ai_confirm: {e}"); return None

async def ai_reply(text: str, memory: str = "",
                   soft_tasks: list[str] | None = None) -> str | None:
    sys_parts = ["Ты — умный личный ассистент в Telegram. Отвечай по-русски."]
    if memory:
        sys_parts.append(f"\nЧТО ТЫ ЗНАЕШЬ О ПОЛЬЗОВАТЕЛЕ (следуй этому):\n{memory}")
    sys_parts.append("\nПравила: следуй инструкциям пользователя из памяти. "
                     "Отвечай кратко если не просят длинного.")
    user_msg = text
    if soft_tasks:
        tasks = "\n".join(f"• {t}" for t in soft_tasks)
        user_msg = f"{text}\n\n[Мягко напомни в конце про задачи:\n{tasks}]"
    try:
        return (await asyncio.to_thread(
            _call, user_msg, "\n".join(sys_parts), False, 0.75
        )).strip()
    except Exception as e:
        log.error(f"ai_reply: {e}"); return None

async def ai_plan(activities: str, memory: str = "") -> str | None:
    now = datetime.now(TZ)
    prompt = (
        f"{'О пользователе: ' + memory + chr(10) if memory else ''}"
        f"Сейчас: {now.strftime('%H:%M, %d.%m.%Y')}\n"
        f"Пользователь хочет сегодня: {activities}\n\n"
        f"Составь умный план на оставшийся день с временны́ми слотами. "
        f"Расставь дела в правильном порядке, учти перерывы. "
        f"Формат: 🕒 ЧЧ:ММ — Дело (краткий комментарий)\n"
        f"В конце — одна мотивационная строка. Без лишних слов."
    )
    try:
        return (await asyncio.to_thread(_call, prompt, None, False, 0.6)).strip()
    except Exception as e:
        log.error(f"ai_plan: {e}"); return None

async def ai_quote(example: str) -> str:
    prompt = (f'Создай вдохновляющую цитату на русском в стиле: "{example}". '
              "Верни ТОЛЬКО текст, без автора и кавычек.")
    try:
        return (await asyncio.to_thread(_call, prompt, None, False, 0.9)).strip().strip("\"'")
    except Exception as e:
        log.error(f"ai_quote: {e}"); return "Каждый день — новый шанс стать лучше!"

# ══════════════════════════════════════════════════════════════
#  ПЛАНИРОВЩИК
# ══════════════════════════════════════════════════════════════
async def tick(bot):
    now     = datetime.now(TZ)
    today_s = now.strftime("%Y-%m-%d")
    today_d = now.date()
    for item in db_due(now.hour, now.minute):
        if item.get("type") == "soft_task":
            continue
        interval  = int(item.get("interval_days") or 1)
        last_sent = item.get("last_sent_date")
        if last_sent and interval > 1:
            try:
                last_d = datetime.strptime(str(last_sent)[:10], "%Y-%m-%d").date()
                if (today_d - last_d).days < interval:
                    continue
            except Exception:
                pass
        cid = int(item["chat_id"])
        t   = item.get("type", "")
        try:
            if t == "reminder":
                await bot.send_message(cid, f"❗ Напоминание: {item['text']}")
                db_mark_sent(item["id"], today_s)
                if not item.get("is_daily"):
                    db_off(item["id"], cid)
            elif t == "note":
                await bot.send_message(cid, f"❗ Не забудь: {item['text']}")
                db_mark_sent(item["id"], today_s)
            elif t == "habit":
                await bot.send_message(cid, f"❗ Время привычки: {item['text']}")
                db_mark_sent(item["id"], today_s)
            elif t == "quote_config":
                q = await ai_quote(item["text"])
                await bot.send_message(cid, f"✨ *Цитата дня:*\n\n{q}", parse_mode="Markdown")
                db_mark_sent(item["id"], today_s)
        except Exception as e:
            log.error(f"tick {item.get('id')}: {e}")

# ──────────────── КЛАВИАТУРА ───────────────────────────────────
def kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Задачи",      callback_data="list"),
         InlineKeyboardButton("☀️ Сегодня",     callback_data="today")],
        [InlineKeyboardButton("⏰ Напоминания", callback_data="reminders"),
         InlineKeyboardButton("💪 Привычки",    callback_data="habits")],
        [InlineKeyboardButton("📝 Заметки",     callback_data="notes"),
         InlineKeyboardButton("🧠 Память",      callback_data="memory")],
        [InlineKeyboardButton("📅 План дня",    callback_data="plan"),
         InlineKeyboardButton("✨ Цитата",      callback_data="quote")],
    ])

# ──────────────── ТЕКСТЫ ──────────────────────────────────────
FALLBACK = "❌ Бот не смог ответить. Попробуй ещё раз."

WELCOME = (
    "👋 Привет! Я твой личный ассистент.\n\n"
    "Ниже — кнопки быстрого доступа.\n"
    "Команды доступны через кнопку *Меню* 📋 в поле ввода.\n\n"
    "*Префиксная система — пиши в любом формате:*\n"
    "┌ `(НАПОМНИ) в 12:03 выпить воды`\n"
    "├ `(ПАМЯТЬ) обращайся ко мне по имени Паша`\n"
    "├ `(ЗАМЕТКА) купить молоко`\n"
    "├ `(ЗАДАЧА) сдать ДЗ до среды`\n"
    "├ `(ПЛАН) отжаться, погулять, магазин`\n"
    "└ `(ПРИВЫЧКА) каждый день в 7:00 зарядка`\n\n"
    "Или просто пиши как обычно — я пойму 😊"
)

HELP_TEXT = (
    "📖 *Полная справка*\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "*Префиксная система*\n"
    "_(КЛЮЧЕВОЕ СЛОВО) текст_\n"
    "или _КЛЮЧЕВОЕ СЛОВО: текст_\n\n"
)

for _k, _v in PREFIX_HELP.items():
    HELP_TEXT += f"• `({_k})` — {_v}\n"

HELP_TEXT += (
    "\n━━━━━━━━━━━━━━━━━━━━\n"
    "*Разделы (команды и кнопки)*\n"
    "📋 /list — все задачи\n"
    "⏰ /reminders — напоминания\n"
    "📝 /notes — заметки\n"
    "💪 /habits — привычки\n"
    "🧠 /memory — память  |  /forget — очистить\n"
    "☀️ /today — сегодня\n"
    "📅 /plan — составить план\n"
    "✨ /quote — цитата\n"
    "🗑 /delete [номер] — удалить\n"
    "🔑 /myid — мой ID\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "*Примеры*\n"
    "— _Напомни в 12:03 выпить воды_\n"
    "— _Каждые 2 дня в 20:00 откачать воду_\n"
    "— _Запомни что меня зовут Павел_\n"
    "— _Составь план: отжаться, погулять_\n"
    "— _Как дела?_ — просто поговори 😊"
)

# ══════════════════════════════════════════════════════════════
#  КОМАНДЫ — ПРОСМОТР РАЗДЕЛОВ
# ══════════════════════════════════════════════════════════════
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    await u.effective_message.reply_text(WELCOME, parse_mode="Markdown", reply_markup=kbd())

async def cmd_help(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    await u.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def cmd_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    await u.effective_message.reply_text("Выбери раздел:", reply_markup=kbd())

async def cmd_myid(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = u.effective_user
    await u.effective_message.reply_text(
        f"🔑 *Твой Telegram ID:*\n`{user.id}`\n\n"
        f"Имя: {user.first_name or '—'}\nUsername: @{user.username or '—'}",
        parse_mode="Markdown",
    )

async def cmd_memory(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    facts = mem_load(u.effective_chat.id)
    if not facts:
        await u.effective_message.reply_text(
            "🧠 *Память пуста.*\n\n"
            "Как записать:\n"
            "• `(ПАМЯТЬ) текст`\n"
            "• Или просто: _Запомни что меня зовут Паша_\n\n"
            "_Память используется как контекст в каждом ответе_",
            parse_mode="Markdown",
        )
        return
    await u.effective_message.reply_text(
        f"🧠 *Что я помню о тебе:*\n\n"
        f"```\n{facts}\n```\n\n"
        f"_Добавить: `(ПАМЯТЬ) текст`_\n"
        f"_Очистить: /forget_",
        parse_mode="Markdown",
    )

async def cmd_forget(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    mem_clear(u.effective_chat.id)
    await u.effective_message.reply_text("🧹 Память очищена.")

async def cmd_plan(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    await u.effective_message.reply_text(
        "📅 Напиши что хочешь сделать сегодня и я составлю план.\n"
        "_Пример: `(ПЛАН) отжаться, погулять, сходить в магазин`_",
        parse_mode="Markdown",
    )

async def cmd_reminders(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Раздел: только напоминания."""
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = db_get(cid, "reminder")
    if not items:
        await u.effective_message.reply_text(
            "⏰ *Напоминаний пока нет.*\n\n"
            "Как добавить:\n"
            "• `(НАПОМНИ) в 12:03 выпить воды`\n"
            "• _Напоминай каждые 2 дня в 20:00 откачать воду_",
            parse_mode="Markdown",
        )
        return
    lines = ["⏰ *Напоминания:*\n"]
    for it in items:
        h  = it.get("remind_hour")
        m_ = it.get("remind_minute", 0)
        iv = int(it.get("interval_days") or 1)
        ts = f" _{h:02d}:{m_:02d}_" if h is not None else ""
        ds = (f" _(каждые {iv} дн.)_" if iv > 1
              else " _(ежедн.)_" if it.get("is_daily") else " _(один раз)_")
        lines.append(f"  `{it['id']}` — {it['text']}{ts}{ds}")
    lines.append("\n_/delete [номер] — удалить_")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_notes(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Раздел: только заметки."""
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = db_get(cid, "note")
    if not items:
        await u.effective_message.reply_text(
            "📝 *Заметок пока нет.*\n\n"
            "Как добавить:\n"
            "• `(ЗАМЕТКА) купить молоко`\n"
            "• _Запомни купить краску_",
            parse_mode="Markdown",
        )
        return
    lines = ["📝 *Заметки:*\n_(напоминаю каждый день в 9:00)_\n"]
    for it in items:
        lines.append(f"  `{it['id']}` — {it['text']}")
    lines.append("\n_/delete [номер] — удалить_")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_habits(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Раздел: только привычки."""
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = db_get(cid, "habit")
    if not items:
        await u.effective_message.reply_text(
            "💪 *Привычек пока нет.*\n\n"
            "Как добавить:\n"
            "• `(ПРИВЫЧКА) каждый день в 7 утра зарядка`\n"
            "• _Хочу каждый день делать зарядку_",
            parse_mode="Markdown",
        )
        return
    lines = ["💪 *Привычки:*\n"]
    for it in items:
        h  = it.get("remind_hour", 8)
        m_ = it.get("remind_minute", 0)
        iv = int(it.get("interval_days") or 1)
        ds = f"каждые {iv} дн." if iv > 1 else "каждый день"
        lines.append(f"  `{it['id']}` — {it['text']} _{h:02d}:{m_:02d}_ ({ds})")
    lines.append("\n_/delete [номер] — удалить_")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_list(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid  = u.effective_chat.id
    all_ = db_get(cid)
    hard = [i for i in all_ if i.get("type") != "soft_task"]
    soft = [i for i in all_ if i.get("type") == "soft_task"]

    if not hard and not soft:
        await u.effective_message.reply_text("Задач пока нет 😊")
        return

    groups = {
        "reminder":     ("⏰ Напоминания",  []),
        "habit":        ("💪 Привычки",     []),
        "note":         ("📝 Заметки",      []),
        "quote_config": ("✨ Цитаты",       []),
    }
    for it in hard:
        k = it.get("type", "reminder")
        if k in groups:
            groups[k][1].append(it)

    lines = ["📋 *Все задачи:*\n"]
    for _, (label, grp) in groups.items():
        if not grp: continue
        lines.append(f"*{label}:*")
        for it in grp:
            h  = it.get("remind_hour")
            m_ = it.get("remind_minute", 0)
            iv = int(it.get("interval_days") or 1)
            ts = f" _{h:02d}:{m_:02d}_" if h is not None else ""
            ds = (f" _(каждые {iv} дн.)_" if iv > 1
                  else " _(ежедн.)_" if it.get("is_daily") else "")
            lines.append(f"  `{it['id']}` — {it['text']}{ts}{ds}")
        lines.append("")

    if soft:
        lines.append("*📌 Задачи-напоминалки:*")
        for it in soft:
            lines.append(f"  `{it['id']}` — {it['text']}")
        lines.append("")

    lines.append("_/delete [номер] — удалить_")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_today(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid   = u.effective_chat.id
    items = [i for i in db_get(cid) if i.get("type") != "soft_task"]
    if not items:
        await u.effective_message.reply_text("Задач нет. Отличный день! ☀️")
        return
    now_    = datetime.now(TZ)
    emj     = {"reminder":"⏰","habit":"💪","note":"📝","quote_config":"✨"}
    timed   = sorted([i for i in items if i.get("remind_hour") is not None],
                     key=lambda x: (x["remind_hour"], x.get("remind_minute", 0)))
    untimed = [i for i in items if i.get("remind_hour") is None]
    lines   = [f"☀️ *{now_.strftime('%d.%m.%Y')}:*\n"]
    for it in timed:
        h, m_ = it["remind_hour"], it.get("remind_minute", 0)
        iv     = int(it.get("interval_days") or 1)
        sfx    = f" _(каждые {iv} дн.)_" if iv > 1 else ""
        lines.append(f"{emj.get(it['type'],'•')} *{h:02d}:{m_:02d}* — {it['text']}{sfx}")
    if untimed:
        lines.append("\n📌 *Без времени:*")
        for it in untimed:
            lines.append(f"  {emj.get(it['type'],'•')} {it['text']}")
    await u.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_delete(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    args = ctx.args
    if not args or not args[0].isdigit():
        await u.effective_message.reply_text(
            "Укажи номер: `/delete 5`\nСписок: /list", parse_mode="Markdown"
        )
        return
    db_off(int(args[0]), u.effective_chat.id)
    await u.effective_message.reply_text(f"✅ Задача #{args[0]} удалена!")

async def cmd_quote(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return
    cid  = u.effective_chat.id
    cfgs = db_get(cid, "quote_config")
    ex   = cfgs[0]["text"] if cfgs else "Каждый день — шанс стать лучше"
    await u.effective_message.reply_chat_action("typing")
    q = await ai_quote(ex)
    await u.effective_message.reply_text(f"✨ *Цитата:*\n\n{q}", parse_mode="Markdown")

# ─────────────── CALLBACK ─────────────────────────────────────
async def on_callback(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    d = q.data
    if d == "myid":       await cmd_myid(u, ctx);      return
    if not await check_access(u): return
    dispatch = {
        "list":      cmd_list,      "today":     cmd_today,
        "reminders": cmd_reminders, "notes":     cmd_notes,
        "habits":    cmd_habits,    "memory":    cmd_memory,
        "plan":      cmd_plan,      "quote":     cmd_quote,
        "help":      cmd_help,
    }
    fn = dispatch.get(d)
    if fn:
        await fn(u, ctx)

# ══════════════════════════════════════════════════════════════
#  ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ══════════════════════════════════════════════════════════════
async def on_msg(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_access(u): return

    cid    = u.effective_chat.id
    text   = u.message.text
    memory = mem_load(cid)
    await u.message.reply_chat_action("typing")

    # ── 1. Проверяем префикс ПЕРВЫМ ─────────────────────────────
    prefix_action, prefix_body = parse_prefix(text)

    if prefix_action:
        # Префикс найден — идём напрямую, без AI-парсинга
        action  = prefix_action
        content = prefix_body
        hints   = _regex_hints(prefix_body)
        hour          = hints.get("hour")
        minute        = hints.get("minute", 0)
        daily         = hints.get("is_daily", False)
        interval_days = hints.get("interval_days", 1)
        intent        = {"action": action, "text": content,
                         "hour": hour, "minute": minute,
                         "is_daily": daily, "interval_days": interval_days,
                         "memory_fact": content,
                         "plan_activities": content}
    else:
        # ── 2. Обычный AI-парсинг ────────────────────────────────
        intent        = await ai_parse(text)
        action        = intent.get("action") or "chat"
        content       = (intent.get("text") or "").strip() or _extract_text(action, text)
        hour          = intent.get("hour")
        minute        = int(intent.get("minute") or 0)
        daily         = bool(intent.get("is_daily", False))
        interval_days = int(intent.get("interval_days") or 1)

    # ── РОУТИНГ ────────────────────────────────────────────────

    if action == "add_reminder":
        if hour is None:
            reply = await ai_confirm(
                "пользователь хочет напоминание но не указал время — спроси дружелюбно", memory
            )
            await u.message.reply_text(
                reply or "❓ В какое время напомнить?\n_Пример: (НАПОМНИ) в 18:00 почистить зубы_",
                parse_mode="Markdown",
            )
            return
        db_add(cid, "reminder", content, int(hour), minute, daily, interval_days)
        freq = (f"каждые {interval_days} дн." if interval_days > 1
                else "каждый день" if daily else "один раз")
        reply = await ai_confirm(
            f"добавил напоминание '{content}' в {int(hour):02d}:{minute:02d} ({freq})", memory
        )
        await u.message.reply_text(
            reply or f"✅ Напомню в *{int(hour):02d}:{minute:02d}* ({freq}):\n_{content}_",
            parse_mode="Markdown",
        )

    elif action == "add_note":
        db_add(cid, "note", content, 9, 0, True)
        reply = await ai_confirm(f"добавил заметку '{content}', напомню каждый день в 9:00", memory)
        await u.message.reply_text(
            reply or f"📝 Записал! Напомню каждый день в *9:00*:\n_{content}_",
            parse_mode="Markdown",
        )

    elif action == "add_habit":
        h = int(hour or 8)
        db_add(cid, "habit", content, h, minute, True, interval_days)
        freq = f"каждые {interval_days} дн." if interval_days > 1 else "каждый день"
        reply = await ai_confirm(
            f"добавил привычку '{content}' в {h:02d}:{minute:02d} ({freq})", memory
        )
        await u.message.reply_text(
            reply or f"💪 Привычка добавлена! {freq.capitalize()} в *{h:02d}:{minute:02d}*:\n_{content}_",
            parse_mode="Markdown",
        )

    elif action == "add_memory":
        fact = (intent.get("memory_fact") or content).strip()
        if fact:
            mem_add_fact(cid, fact)
            memory = mem_load(cid)
            reply  = await ai_confirm(f"запомнил факт: '{fact}'", memory)
            await u.message.reply_text(
                reply or f"🧠 Запомнил!\n`{fact}`\n\n_Посмотреть: /memory_",
                parse_mode="Markdown",
            )
        else:
            await u.message.reply_text("🤔 Не понял что запомнить. Попробуй ещё раз.")

    elif action == "add_soft_task":
        soft_add(cid, content)
        reply = await ai_confirm(
            f"запомнил задачу '{content}', буду напоминать каждые ~8 часов в ответах", memory
        )
        await u.message.reply_text(
            reply or f"📌 Запомнил!\n_{content}_\n\nБуду мягко напоминать в ответах (~8ч).",
            parse_mode="Markdown",
        )

    elif action == "show_memory":
        await cmd_memory(u, ctx)

    elif action == "clear_memory":
        mem_clear(cid)
        await u.message.reply_text("🧹 Память очищена.")

    elif action == "list":
        await cmd_list(u, ctx)

    elif action == "delete":
        did = intent.get("delete_id")
        if did:
            db_off(int(did), cid)
            reply = await ai_confirm(f"удалил задачу #{did}", memory)
            await u.message.reply_text(reply or f"✅ Задача #{did} удалена!")
        else:
            await u.message.reply_text(
                "Укажи номер: _удали номер 5_\nСписок: /list", parse_mode="Markdown"
            )

    elif action == "set_quote":
        ex = intent.get("quote_example") or content or "Каждый день — новый шанс"
        for old in db_get(cid, "quote_config"):
            db_off(old["id"], cid)
        db_add(cid, "quote_config", ex, 8, 0, True)
        reply = await ai_confirm(f"настроил ежедневные цитаты в 8:00 в стиле '{ex}'", memory)
        await u.message.reply_text(
            reply or f"✨ Буду присылать цитаты каждый день в *8:00*!\nСтиль: _{ex}_",
            parse_mode="Markdown",
        )

    elif action == "make_plan":
        activities = intent.get("plan_activities") or content
        plan = await ai_plan(activities, memory)
        if plan is None:
            await u.message.reply_text(FALLBACK); return
        await u.message.reply_text(f"📅 *План на сегодня:*\n\n{plan}", parse_mode="Markdown")

    else:  # chat
        soft  = soft_pending(cid)
        reply = await ai_reply(text, memory, soft if soft else None)
        await u.message.reply_text(reply or FALLBACK)

# ══════════════════════════════════════════════════════════════
#  POST_INIT — меню + команды
# ══════════════════════════════════════════════════════════════
async def post_init(app: Application):
    commands = [
        BotCommand("start",     "🏠 Главное меню"),
        BotCommand("list",      "📋 Все задачи"),
        BotCommand("reminders", "⏰ Напоминания"),
        BotCommand("notes",     "📝 Заметки"),
        BotCommand("habits",    "💪 Привычки"),
        BotCommand("memory",    "🧠 Моя память"),
        BotCommand("today",     "☀️ На сегодня"),
        BotCommand("plan",      "📅 Составить план дня"),
        BotCommand("quote",     "✨ Цитата дня"),
        BotCommand("forget",    "🧹 Очистить память"),
        BotCommand("delete",    "🗑 Удалить задачу"),
        BotCommand("myid",      "🔑 Мой Telegram ID"),
        BotCommand("help",      "📖 Справка"),
    ]

    # Устанавливаем команды глобально
    await app.bot.set_my_commands(commands)

    # Кнопка «Меню» в поле ввода → открывает список команд
    try:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        log.info("✅ MenuButtonCommands установлен")
    except Exception as e:
        log.warning(f"set_chat_menu_button: {e}")

    log.info(f"✅ Команды зарегистрированы ({len(commands)} шт.)")

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
async def main():
    threading.Thread(target=start_flask, daemon=True).start()
    log.info(f"Flask :{PORT}")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    cmd_handlers = [
        ("start",     cmd_start),   ("help",    cmd_help),
        ("menu",      cmd_menu),    ("myid",    cmd_myid),
        ("list",      cmd_list),    ("today",   cmd_today),
        ("plan",      cmd_plan),    ("memory",  cmd_memory),
        ("forget",    cmd_forget),  ("delete",  cmd_delete),
        ("quote",     cmd_quote),   ("reminders", cmd_reminders),
        ("notes",     cmd_notes),   ("habits",  cmd_habits),
    ]
    for cmd, fn in cmd_handlers:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))

    sched = AsyncIOScheduler(timezone=TZ)
    sched.add_job(tick, IntervalTrigger(minutes=1),
                  args=[app.bot], id="tick", max_instances=1)
    sched.start()

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    log.info(f"✅ Bot running  model={MODEL}")

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

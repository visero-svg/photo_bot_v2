import asyncio
import logging
import os
import sqlite3
import threading
import random
from datetime import datetime, time
from pathlib import Path

from flask import Flask, render_template, jsonify
from telethon import TelegramClient, events, functions, errors
from telethon.sessions import StringSession, SQLiteSession

# ======================= НАСТРОЙКИ (через ENV) ======================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING", "")
PHONE = os.environ.get("PHONE")
OWNER_ID = int(os.environ["OWNER_ID"])
NOTIFY_CHANNEL = os.environ.get("NOTIFY_CHANNEL")
if NOTIFY_CHANNEL and NOTIFY_CHANNEL.strip():
    NOTIFY_CHANNEL = int(NOTIFY_CHANNEL)
else:
    NOTIFY_CHANNEL = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

KEYWORDS_FILE = DATA_DIR / "keywords.txt"
CHANNELS_FILE = BASE_DIR / "channels.txt"
RIGHTS_CHECK_FILE = DATA_DIR / "проверка_прав.txt"
DB_PATH = DATA_DIR / "log.db"
# =========================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
app = Flask(__name__)

def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            keyword TEXT,
            user TEXT,
            chat TEXT,
            chat_id INTEGER,
            text TEXT,
            link TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS channel_stats (
            chat_id INTEGER PRIMARY KEY,
            chat_title TEXT,
            total_hits INTEGER DEFAULT 0,
            last_hit TEXT
        )
    """)
    conn.commit()
    return conn

if SESSION_STRING:
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    client = TelegramClient(SQLiteSession(str(DATA_DIR / "session")), API_ID, API_HASH)

EXCLUDE_WORDS = {"фуд", "интерьерный", "фуд-фото", "интерьерная", "фудсъемка"}

RESPONSES = [
    "Добрый день! Я фотограф из Москвы (Мытищи/Королёв/Пушкино). Работаю по всему Подмосковью.\n"
    "Примеры работ: https://aleksviphoto.ru/\n"
    "Буду рад обсудить вашу съёмку — пишите прямо здесь!",

    "Привет! Вижу, вы ищете фотографа. Я из Подмосковья, снимаю свадьбы, репортажи, семейные съёмки.\n"
    "Портфолио: https://aleksviphoto.ru/\n"
    "Напишите, что нужно — подберу удобное время и бюджет",

    "Здравствуйте! Я фотограф-репортажник из Москвы. Готов приехать в любой район.\n"
    "Работы: https://aleksviphoto.ru/\n"
    "Пишите — всё обсудим!"
]

def load_keywords():
    if KEYWORDS_FILE.exists():
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return {line.strip().lower() for line in f if line.strip()}
    default = {
        "ищу фотографа", "нужен фотограф", "требуется фотограф", "ищу съемку",
        "нужна съемка", "фотосессия", "фотограф репортажный", "фотограф в москве",
        "фотограф москва", "фотограф мытищи", "фотограф королев", "фотограф пушкино",
        "фотограф подмосковье", "бюджет", "цена договорная", "ищу исполнителя"
    }
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        for kw in default:
            f.write(kw + "\n")
    return default

def is_working_time() -> bool:
    now = datetime.now().time()
    return time(8, 0) <= now < time(22, 0)

# ======================= ВЕБ =======================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/matches")
def api_matches():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT time,keyword,user,chat,text,link FROM matches ORDER BY id DESC LIMIT 300")
    rows = c.fetchall()
    conn.close()
    return jsonify([{
        "time": r[0][-8:-3] if r[0] else "",
        "keyword": r[1],
        "user": r[2],
        "chat": r[3],
        "text": (r[4][:140] + "...") if r[4] and len(r[4]) > 140 else (r[4] or ""),
        "link": r[5]
    } for r in rows])

@app.route("/api/channel_stats")
def api_channel_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT chat_title, total_hits, last_hit FROM channel_stats ORDER BY total_hits DESC")
    rows = c.fetchall()
    conn.close()
    return jsonify([{
        "title": r[0] or "Неизвестно",
        "hits": r[1],
        "last_hit": r[2][-8:-3] if r[2] else "Никогда"
    } for r in rows])

@app.route("/api/total_stats")
def api_total_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM matches")
    total_matches = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT chat_id) FROM matches WHERE chat_id < 0")
    active_channels = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT chat_id) FROM channel_stats WHERE total_hits > 0")
    channels_with_hits = c.fetchone()[0]
    conn.close()
    return jsonify({
        "total_matches": total_matches,
        "active_channels": active_channels,
        "channels_with_hits": channels_with_hits
    })

# ======================= ПРОВЕРКА ПРАВ В КАНАЛАХ =======================
async def check_bot_rights():
    logging.info("Автоматическая проверка прав в каналах...")
    report = [f"ПРОВЕРКА ПРАВ — {datetime.now():%d.%m.%Y %H:%M}\n"]
    report.append("=" * 70 + "\n")

    ok_count = 0
    problem_count = 0

    if not CHANNELS_FILE.exists():
        report.append("channels.txt не найден\n")
        with open(RIGHTS_CHECK_FILE, "w", encoding="utf-8") as f:
            f.write("".join(report))
        return

    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        links = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    for link in links:
        try:
            entity = await client.get_entity(link)
            async for _ in client.iter_messages(entity, limit=1):
                report.append(f"ОК — вижу сообщения: {link}\n")
                ok_count += 1
                break
            else:
                report.append(f"ОК — канал пустой: {link}\n")
                ok_count += 1
        except errors.ChatAdminRequiredError:
            report.append(f"НЕТ ПРАВ ЧИТАТЬ: {link}\n")
            problem_count += 1
        except errors.ChannelPrivateError:
            report.append(f"ПРИВАТНЫЙ: {link}\n")
            problem_count += 1
        except (errors.UsernameInvalidError, errors.ChannelInvalidError):
            report.append(f"КАНАЛ УДАЛЁН: {link}\n")
            problem_count += 1
        except Exception as e:
            report.append(f"ОШИБКА: {link} → {str(e)[:50]}\n")
            problem_count += 1
        await asyncio.sleep(1)

    report.append("=" * 70 + "\n")
    report.append(f"Работает: {ok_count} | Проблем: {problem_count}\n")

    with open(RIGHTS_CHECK_FILE, "w", encoding="utf-8") as f:
        f.write("".join(report))

    logging.info(f"Проверка завершена: {ok_count} OK, {problem_count} проблем")

    if problem_count > 0:
        try:
            await client.send_message(
                OWNER_ID,
                f"Проверка прав завершена\n"
                f"Работает: {ok_count} | Проблем: {problem_count}\n"
                f"Подробности: data/проверка_прав.txt"
            )
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление: {e}")

# ======================= ПОДПИСКА ПО КОМАНДЕ /list =======================
@client.on(events.NewMessage(pattern=r"^/list$", from_users=OWNER_ID))
async def cmd_list(event):
    if not CHANNELS_FILE.exists():
        await event.reply("channels.txt не найден")
        return

    await event.reply("Начинаю подписку по списку из channels.txt...")

    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        all_links = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    remaining = []
    joined = 0
    processed = 0

    logging.info(f"Ручная подписка (/list): {len(all_links)} каналов")

    for link in all_links:
        try:
            await asyncio.wait_for(
                client(functions.channels.JoinChannelRequest(channel=link)),
                timeout=15.0
            )
            joined += 1
            processed += 1
            logging.info(f"Подписан ({joined}): {link}")

            if processed % 30 == 0:
                pause = random.randint(300, 1200)
                logging.info(f"Большая пауза {pause // 60} мин...")
                await asyncio.sleep(pause)

            await asyncio.sleep(random.randint(40, 100))

        except (errors.UsernameInvalidError,
                errors.ChannelInvalidError,
                errors.ChatWriteForbiddenError,
                errors.ChannelPrivateError,
                errors.UserAlreadyParticipantError):
            logging.info(f"Мёртвый/приватный/уже подписан — УДАЛЯЮ: {link}")
            continue

        except asyncio.TimeoutError:
            logging.info(f"Тайм-аут — УДАЛЯЮ: {link}")
            continue

        except errors.FloodWaitError as e:
            logging.warning(f"Флуд! Жду {e.seconds} сек")
            await event.reply(f"Флуд-контроль: жду {e.seconds} сек. Подписано: {joined}")
            await asyncio.sleep(e.seconds + 5)
            remaining.append(link)

        except Exception as e:
            if "No user has" in str(e) or "Cannot find" in str(e):
                logging.info(f"Канал не существует — УДАЛЯЮ: {link}")
                continue
            logging.warning(f"Ошибка: {e} — оставляю")
            remaining.append(link)

    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        for link in remaining:
            f.write(link + "\n")

    msg = f"Готово! Подписано: {joined}. Осталось в списке: {len(remaining)}"
    logging.info(msg)
    await event.reply(msg)

# ======================= ОБРАБОТЧИК СООБЩЕНИЙ =======================
@client.on(events.NewMessage)
async def handler(event):
    if not event.message or not event.message.message:
        return

    # Игнорируем команды
    if event.message.message.startswith("/"):
        return

    text = event.message.message.lower()
    current_keywords = load_keywords()

    if any(ex in text for ex in EXCLUDE_WORDS):
        return
    if not any(kw in text for kw in current_keywords):
        return

    sender = await event.get_sender()
    if not sender or getattr(sender, "bot", False):
        return

    chat = await event.get_chat()
    chat_id = getattr(chat, "id", None)
    chat_title = getattr(chat, "title", "Личка")
    user_name = f"{sender.first_name or ''} {sender.last_name or ''}".strip() or "Аноним"

    link = "Личка"
    if chat_id and chat_id < 0:
        link = f"https://t.me/c/{abs(chat_id)}/{event.message.id}"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO matches (time,keyword,user,chat,chat_id,text,link) VALUES (?,?,?,?,?,?,?)",
            (now_str, "найдено", user_name, chat_title, chat_id, text, link)
        )
        if chat_id and chat_id < 0:
            c.execute("""
                INSERT INTO channel_stats (chat_id, chat_title, total_hits, last_hit)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    total_hits = total_hits + 1,
                    last_hit = excluded.last_hit,
                    chat_title = excluded.chat_title
            """, (chat_id, chat_title, now_str))
        conn.commit()
    except Exception as e:
        logging.error(f"Ошибка записи в БД: {e}")
    finally:
        conn.close()

    try:
        await client.forward_messages(OWNER_ID, event.message)
        if NOTIFY_CHANNEL:
            await client.send_message(
                NOTIFY_CHANNEL,
                f"Найдено в {chat_title}!\nОт: {user_name}\n[Перейти]({link})",
                parse_mode="md"
            )
    except Exception as e:
        logging.error(f"Ошибка уведомления: {e}")

    if event.is_private and is_working_time():
        try:
            await asyncio.sleep(random.uniform(7, 18))
            await event.reply(random.choice(RESPONSES))
        except Exception as e:
            logging.warning(f"Не ответил: {e}")

# ======================= ЗАПУСК =======================
def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

async def main():
    logging.info("Фото-бот запущен | Koyeb")
    load_keywords()
    get_db_connection().close()

    if SESSION_STRING:
        await client.start()
    else:
        await client.start(phone=PHONE)

    await check_bot_rights()

    threading.Thread(target=run_web, daemon=True).start()

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
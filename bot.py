import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google import genai
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ==================================================
# НАСТРОЙКИ
# ==================================================

# Сначала берутся переменные окружения, если их нет — значения ниже.
BOT_TOKEN = os.getenv("BOT_TOKEN", "9005048762:AAHNZ3WyJKMk9d7SaN1XsKi8R52cMUSAklA")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6IS4Mi_JUvjokkYVrUcDON3KSnEIUsHvt3hN1X5bbX0rQ")

ADMIN_ID = 8256340679

TIMEZONE = ZoneInfo("Europe/Moscow")

SCAN_HOUR = 6
SCAN_MINUTE = 30

MAX_VIDEOS = 100          # общий лимит за один скан
PER_SOURCE_LIMIT = 15     # лимит на один хештег / ленту
SCROLLS_PER_PAGE = 10

PROFILE_DIR = "tiktok_profile"
DATABASE = "scout.db"

HASHTAGS = [
    "мото",
    "мотоцикл",
    "мотозапчасти",
    "мотообзор",
    "мотоэкипировка",
    "мототюнинг",
    "ремонтмото",
    "motorcycle",
    "motorcycleparts",
    "bikelife",
    "motovlog",
]


# ==================================================
# ИНИЦИАЛИЗАЦИЯ
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("DANIL_IRBIS")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

scheduler = AsyncIOScheduler(timezone=TIMEZONE)

scan_lock = asyncio.Lock()
stop_event = asyncio.Event()

ai = None

if GEMINI_API_KEY and not GEMINI_API_KEY.startswith("ВСТАВЬ_"):
    ai = genai.Client(api_key=GEMINI_API_KEY)


class CaptchaError(Exception):
    pass


# ==================================================
# ВХОД В TIKTOK ЧЕРЕЗ TELEGRAM (без ПК)
# ==================================================

login_state = {"pw": None, "ctx": None, "page": None}

BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


def login_active():
    return login_state["ctx"] is not None


async def login_open():
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=True,
        viewport={"width": 1280, "height": 800},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        args=BROWSER_ARGS
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    login_state.update(pw=pw, ctx=ctx, page=page)
    await page.goto(
        "https://www.tiktok.com/login/qrcode",
        wait_until="domcontentloaded",
        timeout=45000
    )
    await asyncio.sleep(5)


async def login_close():
    try:
        if login_state["ctx"]:
            await login_state["ctx"].close()
        if login_state["pw"]:
            await login_state["pw"].stop()
    finally:
        login_state.update(pw=None, ctx=None, page=None)


async def send_shot(chat_id, caption=""):
    page = login_state["page"]
    data = await page.screenshot(type="png")
    await bot.send_photo(
        chat_id,
        BufferedInputFile(data, filename="shot.png"),
        caption=caption
    )


# ==================================================
# БАЗА ДАННЫХ
# ==================================================

def db():
    return closing(sqlite3.connect(DATABASE))


def init_database():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                url TEXT PRIMARY KEY,
                title TEXT,
                source TEXT,
                found_at TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                found INTEGER,
                status TEXT
            )
        """)
        con.commit()


def save_video(video):
    with db() as con:
        cursor = con.execute("""
            INSERT OR IGNORE INTO videos (url, title, source, found_at)
            VALUES (?, ?, ?, ?)
        """, (
            video["url"],
            video["title"],
            video["source"],
            datetime.now(TIMEZONE).isoformat()
        ))
        con.commit()
        return cursor.rowcount == 1


def get_video_count():
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def save_scan_history(found, status):
    with db() as con:
        con.execute("""
            INSERT INTO scan_history(date, found, status)
            VALUES (?, ?, ?)
        """, (
            datetime.now(TIMEZONE).isoformat(),
            found,
            status
        ))
        con.commit()


# ==================================================
# ПРОВЕРКА АДМИНИСТРАТОРА
# ==================================================

def is_admin(message):
    return (
        message.from_user is not None
        and message.from_user.id == ADMIN_ID
    )


async def admin_check(message):
    if not is_admin(message):
        await message.answer("Доступ запрещён.")
        return False
    return True


# ==================================================
# ПРОВЕРКА CAPTCHA И ОКНА ВХОДА
# ==================================================

async def page_text(page):
    try:
        return (await page.locator("body").inner_text(timeout=5000)).lower()
    except Exception:
        return ""


async def detect_captcha(page):
    text = await page_text(page)

    markers = [
        "captcha",
        "verify to continue",
        "security verification",
        "проверка безопасности",
        "подтвердите, что вы не робот",
        "перетащите ползунок",
    ]

    return any(marker in text for marker in markers)


# ==================================================
# СБОР ВИДЕО TIKTOK
# ==================================================

CARDS_JS = """
elements => elements.map(element => {
    const img = element.querySelector('img');
    return {
        url: element.href.split('?')[0],
        title:
            (element.innerText || '') + ' ' +
            (element.getAttribute('aria-label') || '') + ' ' +
            ((img && img.alt) || '')
    };
})
"""


async def collect_videos(page, source, results):
    added = 0

    for _ in range(SCROLLS_PER_PAGE):

        if stop_event.is_set():
            break

        if len(results) >= MAX_VIDEOS or added >= PER_SOURCE_LIMIT:
            break

        if await detect_captcha(page):
            raise CaptchaError()

        try:
            cards = await page.locator(
                'a[href*="/video/"]'
            ).evaluate_all(CARDS_JS)
        except Exception as error:
            logger.warning("Ошибка чтения карточек: %s", error)
            cards = []

        for card in cards:

            url = card.get("url", "")

            if "/video/" not in url or url in results:
                continue

            title = re.sub(r"\s+", " ", card.get("title", "")).strip()

            results[url] = {
                "url": url,
                "title": title[:500] or "Название не найдено",
                "source": source
            }
            added += 1

            if len(results) >= MAX_VIDEOS or added >= PER_SOURCE_LIMIT:
                break

        await page.evaluate("window.scrollBy(0, 1500)")
        await asyncio.sleep(2)


async def scan_source(page, url, source, results):
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(3)

    if await detect_captcha(page):
        raise CaptchaError()

    await collect_videos(page, source, results)


# ==================================================
# ЗАПУСК БРАУЗЕРА
# ==================================================

async def scan_tiktok():
    """Возвращает (список видео, была_ли_captcha). Собранное не теряется."""

    results = {}
    captcha = False

    async with async_playwright() as playwright:

        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )

        try:
            page = (
                context.pages[0]
                if context.pages
                else await context.new_page()
            )

            async def block_heavy_resources(route):
                if route.request.resource_type in ["media", "font"]:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_heavy_resources)

            sources = [
                (f"https://www.tiktok.com/tag/{tag}", f"#{tag}")
                for tag in HASHTAGS
            ]
            sources.append(("https://www.tiktok.com/foryou", "For You"))

            for url, source in sources:

                if stop_event.is_set() or len(results) >= MAX_VIDEOS:
                    break

                logger.info("Сканирую: %s", source)

                try:
                    await scan_source(page, url, source, results)

                except CaptchaError:
                    captcha = True
                    break

                except PlaywrightTimeoutError:
                    logger.warning("Тайм-аут: %s", source)

                except Exception:
                    logger.exception("Ошибка источника %s", source)

        finally:
            await context.close()

    return list(results.values())[:MAX_VIDEOS], captcha


# ==================================================
# АНАЛИЗ GEMINI
# ==================================================

async def analyze_videos(videos):

    if not videos:
        return "Новых видео для анализа нет."

    if not ai:
        return "Gemini не подключён. Проверь GEMINI_API_KEY."

    video_text = "\n".join(
        f"- {video['title']} | {video['url']}"
        for video in videos[:35]
    )

    prompt = f"""
Ты DANIL IRBIS AI SCOUT — аналитик мотоциклетного рынка и TikTok-трендов.

Специализация: мотоциклы, мотозапчасти, мотоэкипировка, ремонт, тюнинг,
аксессуары, мотоновости, мотопутешествия.

Проанализируй найденные TikTok-видео и выдели:
1. Интересные мотоциклетные тренды.
2. Популярные темы.
3. Интересные запчасти и аксессуары.
4. Идеи для публикаций.
5. Видео, которые стоит изучить подробнее.

Не выдумывай просмотры, характеристики и факты.
Если информации недостаточно, прямо сообщи об этом.

Видео:

{video_text}
"""

    try:
        response = await asyncio.to_thread(
            ai.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text or "ИИ не вернул результат."

    except Exception as error:
        logger.exception("Ошибка Gemini")
        return f"Ошибка Gemini: {error}"


# ==================================================
# ОТПРАВКА ДЛИННЫХ СООБЩЕНИЙ
# ==================================================

async def send_long_message(chat_id, text):
    for position in range(0, len(text), 3800):
        await bot.send_message(chat_id, text[position:position + 3800])


# ==================================================
# ГЛАВНОЕ СКАНИРОВАНИЕ
# ==================================================

async def run_scan(chat_id):

    if login_active():

        await bot.send_message(
            chat_id,
            "Открыто окно входа TikTok. Закрой его командой /loginend."
        )

        return

    if scan_lock.locked():
        await bot.send_message(chat_id, "Сканирование уже выполняется.")
        return

    async with scan_lock:

        stop_event.clear()

        await bot.send_message(
            chat_id,
            "🏍 DANIL IRBIS: начинаю сканирование TikTok."
        )

        try:
            videos, captcha = await scan_tiktok()

            new_videos = [v for v in videos if save_video(v)]

            analysis = await analyze_videos(new_videos)

            if captcha:
                status = "captcha"
            elif stop_event.is_set():
                status = "stopped"
            else:
                status = "completed"

            save_scan_history(len(new_videos), status)

            notes = ""
            if captcha:
                notes = (
                    "\n⚠️ TikTok показал CAPTCHA, сканирование прервано. "
                    "Показано то, что успели собрать. Пройди проверку "
                    "вручную через login_tiktok.py. Обход защиты не выполняется.\n"
                )

            report = f"""
🏁 DANIL IRBIS — ОТЧЁТ
{notes}
Найдено видео: {len(videos)}
Новых видео: {len(new_videos)}
Всего в базе: {get_video_count()}

Время: {datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M")}

🤖 АНАЛИЗ GEMINI

{analysis}
"""
            await send_long_message(chat_id, report)

        except Exception as error:
            logger.exception("Ошибка сканирования")
            save_scan_history(0, "error")
            await bot.send_message(chat_id, f"Ошибка сканирования: {error}")


# ==================================================
# TELEGRAM-КОМАНДЫ
# ==================================================

@dp.message(Command("start"))
async def start_command(message: Message):

    if not await admin_check(message):
        return

    await message.answer(
        "🏍 DANIL IRBIS AI SCOUT 2.1\n\n"
        "/scan — запустить сканирование\n"
        "/stop — остановить сканирование\n"
        "/top — последние найденные видео\n"
        "/status — состояние бота\n"
        "/login — войти в TikTok по QR-коду\n"
        "/shot — скриншот окна входа\n"
        "/clicktext, /click, /type, /key — управление окном входа\n"
        "/loginend — завершить вход и сохранить сессию"
    )


@dp.message(Command("login"))
async def login_command(message: Message):

    if not await admin_check(message):
        return

    if scan_lock.locked():
        await message.answer("Сейчас идёт сканирование. Дождись или /stop.")
        return

    if login_active():
        await send_shot(message.chat.id, "Окно входа уже открыто.")
        return

    await message.answer("Открываю страницу входа TikTok, подожди ~10 секунд...")

    try:
        await login_open()
        await send_shot(
            message.chat.id,
            "Вход по QR-коду:\n"
            "1. Открой TikTok на телефоне.\n"
            "2. Профиль → меню (≡) → «Мой QR-код» → сканер.\n"
            "3. Наведи на QR с этой картинки и подтверди вход.\n"
            "4. Пришли /shot, чтобы проверить, что вход прошёл, затем /loginend.\n\n"
            "Если QR не виден: /clicktext QR или /shot."
        )
    except Exception as error:
        logger.exception("Ошибка открытия входа")
        await login_close()
        await message.answer(f"Не удалось открыть страницу входа: {error}")


@dp.message(Command("shot"))
async def shot_command(message: Message):

    if not await admin_check(message):
        return

    if not login_active():
        await message.answer("Окно входа не открыто. Сначала /login.")
        return

    await send_shot(message.chat.id)


@dp.message(Command("clicktext"))
async def clicktext_command(message: Message, command: CommandObject):

    if not await admin_check(message):
        return

    if not login_active() or not command.args:
        await message.answer("Использование: /clicktext Текст кнопки (после /login).")
        return

    try:
        await login_state["page"].get_by_text(
            command.args, exact=False
        ).first.click(timeout=8000)
        await asyncio.sleep(3)
    except Exception as error:
        await message.answer(f"Не нашёл элемент: {error}")

    await send_shot(message.chat.id)


@dp.message(Command("click"))
async def click_command(message: Message, command: CommandObject):

    if not await admin_check(message):
        return

    if not login_active() or not command.args:
        await message.answer("Использование: /click X Y (экран 1280x800).")
        return

    try:
        x, y = (int(v) for v in command.args.split()[:2])
        await login_state["page"].mouse.click(x, y)
        await asyncio.sleep(3)
    except Exception as error:
        await message.answer(f"Ошибка клика: {error}")

    await send_shot(message.chat.id)


@dp.message(Command("type"))
async def type_command(message: Message, command: CommandObject):

    if not await admin_check(message):
        return

    if not login_active() or not command.args:
        await message.answer("Использование: /type текст (после /login).")
        return

    try:
        await login_state["page"].keyboard.type(command.args, delay=60)
    except Exception as error:
        await message.answer(f"Ошибка ввода: {error}")

    # удаляем сообщение, чтобы не оставлять введённые данные в чате
    try:
        await message.delete()
    except Exception:
        pass

    await send_shot(message.chat.id)


@dp.message(Command("key"))
async def key_command(message: Message, command: CommandObject):

    if not await admin_check(message):
        return

    if not login_active() or not command.args:
        await message.answer("Использование: /key Enter (или Tab, Backspace).")
        return

    try:
        await login_state["page"].keyboard.press(command.args.strip())
        await asyncio.sleep(3)
    except Exception as error:
        await message.answer(f"Ошибка: {error}")

    await send_shot(message.chat.id)


@dp.message(Command("loginend"))
async def loginend_command(message: Message):

    if not await admin_check(message):
        return

    if not login_active():
        await message.answer("Окно входа не открыто.")
        return

    await login_close()
    await message.answer("Сессия сохранена в tiktok_profile. Можно запускать /scan.")


@dp.message(Command("scan"))
async def scan_command(message: Message):

    if not await admin_check(message):
        return

    await run_scan(message.chat.id)


@dp.message(Command("stop"))
async def stop_command(message: Message):

    if not await admin_check(message):
        return

    stop_event.set()
    await message.answer("Остановка запрошена. Текущий шаг завершится.")


@dp.message(Command("status"))
async def status_command(message: Message):

    if not await admin_check(message):
        return

    await message.answer(
        "📊 DANIL IRBIS\n\n"
        f"Сканирование: {'идёт' if scan_lock.locked() else 'не запущено'}\n"
        f"Видео в базе: {get_video_count()}\n"
        f"Профиль TikTok: {'найден' if os.path.isdir(PROFILE_DIR) else 'не создан'}\n"
        f"Окно входа: {'открыто' if login_active() else 'закрыто'}\n"
        f"Gemini: {'подключён' if ai else 'не настроен'}\n"
        "Часовой пояс: Europe/Moscow\n"
        f"Автозапуск: ежедневно в {SCAN_HOUR:02d}:{SCAN_MINUTE:02d}"
    )


@dp.message(Command("top"))
async def top_command(message: Message):

    if not await admin_check(message):
        return

    with db() as con:
        rows = con.execute("""
            SELECT title, url, source
            FROM videos
            ORDER BY found_at DESC
            LIMIT 10
        """).fetchall()

    if not rows:
        await message.answer("Пока видео нет. Запусти /scan.")
        return

    report = ["🏆 ПОСЛЕДНИЕ НАЙДЕННЫЕ ВИДЕО"]

    for number, (title, url, source) in enumerate(rows, 1):
        report.append(
            f"\n{number}. {title}\nИсточник: {source}\n{url}"
        )

    await send_long_message(message.chat.id, "\n".join(report))


# ==================================================
# АВТОЗАПУСК
# ==================================================

async def scheduled_scan():
    await run_scan(ADMIN_ID)


# ==================================================
# ЗАПУСК БОТА
# ==================================================

async def main():

    if not BOT_TOKEN or BOT_TOKEN.startswith("ВСТАВЬ_"):
        raise RuntimeError("Необходимо заполнить BOT_TOKEN")

    init_database()

    scheduler.add_job(
        scheduled_scan,
        trigger="cron",
        hour=SCAN_HOUR,
        minute=SCAN_MINUTE,
        timezone=TIMEZONE,
        id="daily_moto_scan",
        replace_existing=True,
        max_instances=1
    )

    scheduler.start()

    logger.info("DANIL IRBIS AI SCOUT запущен")

    try:
        await bot.send_message(
            ADMIN_ID,
            "🏍 DANIL IRBIS AI SCOUT 2.1 запущен."
        )
    except Exception as error:
        logger.warning(
            "Не удалось написать админу (нажми /start у бота): %s", error
        )

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await login_close()
        await bot.session.close()


if __name__ == "__main__":

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

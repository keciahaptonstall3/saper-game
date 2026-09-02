"""
Telegram-бот для мини-игры «Сапёр» (aiogram 3.x) с глобальными функциями.

Возможности:
  • /start [ref_<id>] — приветствие + кнопка игры; засчитывает реферала
  • /play            — открыть игру
  • /invite          — личная реферальная ссылка + счётчик друзей
  • /top             — таблица лидеров в чате
  • /reminders       — вкл/выкл ежедневные напоминания
  • /help            — справка
  • постоянная кнопка-меню «Играть»
  • HTTP-API для игры:  POST /api/score, GET /api/leaderboard  (с проверкой подписи Telegram)
  • ежедневное напоминание неактивным игрокам про бонус

Переменные окружения (см. .env.example):
  BOT_TOKEN   — токен от @BotFather
  WEBAPP_URL  — https-адрес saper_miniapp.html
  PORT        — порт HTTP-API (по умолчанию 8080; на Render/Railway задаётся автоматически)
"""

import asyncio
import datetime as dt
import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MenuButtonWebApp,
    WebAppInfo,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data

import db

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")
PORT = int(os.getenv("PORT", "8080"))
REMINDER_HOUR_UTC = int(os.getenv("REMINDER_HOUR_UTC", "15"))  # 15:00 UTC ≈ 18:00 МСК

if not BOT_TOKEN or not WEBAPP_URL:
    raise SystemExit(
        "Не заданы BOT_TOKEN и/или WEBAPP_URL. Скопируй .env.example в .env и заполни."
    )
if not WEBAPP_URL.startswith("https://"):
    raise SystemExit("WEBAPP_URL должен быть на https:// — Telegram не откроет Mini App по http.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("saper-bot")
dp = Dispatcher()
BOT_USERNAME = ""  # заполнится при старте


def game_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💣 Играть в Сапёр", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


# ------------------------- команды -------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    u = message.from_user
    is_new = await db.add_user(u.id, u.username, u.first_name)

    # реферальная ссылка вида ?start=ref_12345
    if is_new and command.args and command.args.startswith("ref_"):
        try:
            referrer_id = int(command.args[4:])
        except ValueError:
            referrer_id = 0
        if referrer_id and await db.set_referral(u.id, referrer_id):
            try:
                total = await db.referrals_count(referrer_id)
                await message.bot.send_message(
                    referrer_id,
                    f"🎉 По твоей ссылке пришёл новый игрок! Всего приглашено: <b>{total}</b>.",
                )
            except Exception:
                pass

    await message.answer(
        f"Привет, {u.first_name or 'друг'}! 👋\n\n"
        "Это <b>Сапёр</b> — открывай безопасные клетки, отмечай мины флажками и очищай поле. "
        "За победы капают монеты, растёт уровень, а серия побед даёт множитель до ×2.2 🔥\n\n"
        "🎁 Каждый день — бонус за вход\n"
        "🏆 Соревнуйся в общей таблице лидеров — /top\n"
        "👥 Зови друзей — /invite\n\n"
        "Жми кнопку ниже 👇",
        reply_markup=game_keyboard(),
    )


@dp.message(Command("play"))
async def cmd_play(message: Message) -> None:
    await db.touch_active(message.from_user.id)
    await message.answer("Погнали! 💣", reply_markup=game_keyboard())


@dp.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    uid = message.from_user.id
    await db.add_user(uid, message.from_user.username, message.from_user.first_name)
    count = await db.referrals_count(uid)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    await message.answer(
        "👥 <b>Позови друзей в Сапёр!</b>\n\n"
        f"Твоя личная ссылка:\n{link}\n\n"
        f"Уже приглашено друзей: <b>{count}</b>\n\n"
        "Делись ссылкой — каждый, кто зайдёт по ней впервые, засчитается тебе.",
        disable_web_page_preview=True,
    )


@dp.message(Command("top"))
async def cmd_top(message: Message) -> None:
    rows = await db.top_scores(10)
    if not rows:
        await message.answer("Таблица лидеров пока пуста. Сыграй партию — и попадёшь в неё! 💣",
                             reply_markup=game_keyboard())
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ игроков по монетам</b>\n"]
    for i, r in enumerate(rows):
        rank = medals[i] if i < 3 else f"{i+1}."
        name = (r["name"] or "Игрок")[:20]
        lines.append(f"{rank} {name} — <b>{r['coins']:,}</b> 🪙 · ур. {r['level']}".replace(",", " "))
    await message.answer("\n".join(lines), reply_markup=game_keyboard())


@dp.message(Command("reminders"))
async def cmd_reminders(message: Message) -> None:
    # переключатель: /reminders off  или  /reminders on
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].lower() in ("off", "выкл", "0"):
        await db.set_reminders(message.from_user.id, False)
        await message.answer("🔕 Ежедневные напоминания выключены. Вернуть: /reminders on")
    else:
        await db.set_reminders(message.from_user.id, True)
        await message.answer("🔔 Ежедневные напоминания включены. Выключить: /reminders off")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Как играть</b>\n"
        "• Тап по клетке — открыть.\n"
        "• Долгий тап (или 🚩) — флажок на мину.\n"
        "• Цифра — сколько мин рядом.\n"
        "• Открой все безопасные клетки — победа!\n\n"
        "<b>Команды</b>\n"
        "/play — открыть игру\n"
        "/top — таблица лидеров\n"
        "/invite — позвать друзей\n"
        "/reminders — напоминания вкл/выкл",
        reply_markup=game_keyboard(),
    )


# ------------------------- HTTP API для игры -------------------------

@web.middleware
async def cors_mw(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    return resp


def _clamp_int(v, lo, hi, default=0):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _opt_time(v):
    if v is None:
        return None
    return _clamp_int(v, 0, 100000, None)


async def api_score(request: web.Request) -> web.Response:
    """Игра присылает результат. Проверяем подпись Telegram, сохраняем."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    init_data = data.get("initData", "")
    try:
        parsed = safe_parse_webapp_init_data(BOT_TOKEN, init_data)
    except Exception:
        return web.json_response({"error": "bad signature"}, status=403)

    user = parsed.user
    if not user:
        return web.json_response({"error": "no user"}, status=403)

    name = user.first_name or user.username or "Игрок"
    coins = _clamp_int(data.get("coins"), 0, 10_000_000)
    level = _clamp_int(data.get("level"), 1, 1000, 1)
    wins = _clamp_int(data.get("wins"), 0, 1_000_000)
    best = data.get("best") or {}

    await db.add_user(user.id, user.username, user.first_name)
    rank = await db.upsert_score(
        user.id, name, coins, level, wins,
        _opt_time(best.get("easy")), _opt_time(best.get("medium")), _opt_time(best.get("hard")),
    )
    await db.touch_active(user.id)
    total = await db.total_players()
    return web.json_response({"ok": True, "rank": rank, "total": total})


async def api_leaderboard(request: web.Request) -> web.Response:
    limit = _clamp_int(request.query.get("limit"), 1, 100, 20)
    rows = await db.top_scores(limit)
    total = await db.total_players()
    return web.json_response({"top": rows, "total": total})


async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def make_app(bot: Bot) -> web.Application:
    app = web.Application(middlewares=[cors_mw])
    app.router.add_post("/api/score", api_score)
    app.router.add_get("/api/leaderboard", api_leaderboard)
    app.router.add_get("/", api_health)
    # OPTIONS-префлайты
    app.router.add_route("OPTIONS", "/api/score", lambda r: web.Response())
    app.router.add_route("OPTIONS", "/api/leaderboard", lambda r: web.Response())
    return app


# ------------------------- планировщик напоминаний -------------------------

async def reminder_loop(bot: Bot) -> None:
    """Раз в час проверяет время; в заданный час шлёт неактивным игрокам напоминание про бонус."""
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        if now.hour == REMINDER_HOUR_UTC:
            users = await db.inactive_users(hours=22, limit=25)
            for uid in users:
                try:
                    await bot.send_message(
                        uid,
                        "🎁 Тебя ждёт ежедневный бонус в Сапёре! Загляни и забери монеты 💣",
                        reply_markup=game_keyboard(),
                    )
                    await db.mark_reminded(uid)
                    await asyncio.sleep(0.1)  # мягкий темп, чтобы не упереться в лимиты
                except Exception as e:
                    log.warning("reminder to %s failed: %s", uid, e)
                    await db.mark_reminded(uid)
            await asyncio.sleep(3600)  # проспать текущий час
        await asyncio.sleep(600)  # проверять каждые 10 минут


# ------------------------- запуск -------------------------

async def main() -> None:
    global BOT_USERNAME
    await db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    me = await bot.get_me()
    BOT_USERNAME = me.username

    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL))
    )
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="play", description="Открыть игру"),
        BotCommand(command="top", description="Таблица лидеров"),
        BotCommand(command="invite", description="Позвать друзей"),
        BotCommand(command="reminders", description="Напоминания вкл/выкл"),
        BotCommand(command="help", description="Как играть"),
    ])

    # поднять HTTP-API
    app = make_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("HTTP-API слушает порт %s", PORT)

    # фоновая рассылка напоминаний
    asyncio.create_task(reminder_loop(bot))

    log.info("Бот @%s запущен. WEBAPP_URL=%s", BOT_USERNAME, WEBAPP_URL)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")

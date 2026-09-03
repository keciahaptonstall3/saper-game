"""
Слой хранилища на SQLite (через aiosqlite).

Две таблицы:
  users  — игроки, рефералы, активность (для напоминаний)
  scores — лучшие результаты для таблицы лидеров (их присылает игра)
"""

import datetime as dt
from typing import Optional

import aiosqlite

DB_PATH = "saper.db"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                referred_by INTEGER,
                referrals   INTEGER DEFAULT 0,
                joined_at   TEXT,
                last_active TEXT,
                reminded_at TEXT,
                reminders   INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS scores(
                user_id    INTEGER PRIMARY KEY,
                name       TEXT,
                coins      INTEGER DEFAULT 0,
                level      INTEGER DEFAULT 1,
                wins       INTEGER DEFAULT 0,
                best_easy  INTEGER,
                best_medium INTEGER,
                best_hard  INTEGER,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payments(
                charge_id  TEXT PRIMARY KEY,
                user_id    INTEGER,
                coins      INTEGER,
                stars      INTEGER,
                claimed    INTEGER DEFAULT 0,
                created_at TEXT
            );
            """
        )
        await db.commit()


async def add_payment(charge_id: str, user_id: int, coins: int, stars: int) -> bool:
    """Записать оплату (идемпотентно по charge_id). True — если это новая оплата."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO payments(charge_id, user_id, coins, stars, created_at) VALUES(?,?,?,?,?)",
                (charge_id, user_id, coins, stars, _now()),
            )
        except aiosqlite.IntegrityError:
            return False  # уже обработана — двойного начисления не будет
        await db.commit()
        return True


async def claim_payments(user_id: int) -> int:
    """Отдать игре сумму неполученных купленных монет и пометить их полученными."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(coins),0) FROM payments WHERE user_id=? AND claimed=0", (user_id,)
        )
        total = (await cur.fetchone())[0] or 0
        if total:
            await db.execute("UPDATE payments SET claimed=1 WHERE user_id=? AND claimed=0", (user_id,))
            await db.commit()
        return int(total)


async def add_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> bool:
    """Возвращает True, если пользователь новый."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        exists = await cur.fetchone()
        if exists:
            await db.execute(
                "UPDATE users SET username=?, first_name=?, last_active=? WHERE user_id=?",
                (username, first_name, _now(), user_id),
            )
            await db.commit()
            return False
        await db.execute(
            "INSERT INTO users(user_id, username, first_name, joined_at, last_active) VALUES(?,?,?,?,?)",
            (user_id, username, first_name, _now(), _now()),
        )
        await db.commit()
        return True


async def set_referral(user_id: int, referrer_id: int) -> bool:
    """Привязать реферера к новому игроку (один раз, не сам на себя). True — если засчитано."""
    if user_id == referrer_id:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row is None or row[0] is not None:
            return False  # нет пользователя или реферер уже назначен
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,))
        if await cur.fetchone() is None:
            return False  # реферер не зарегистрирован
        await db.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, user_id))
        await db.execute("UPDATE users SET referrals=referrals+1 WHERE user_id=?", (referrer_id,))
        await db.commit()
        return True


async def referrals_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referrals FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def touch_active(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_active=? WHERE user_id=?", (_now(), user_id))
        await db.commit()


async def set_reminders(user_id: int, on: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reminders=? WHERE user_id=?", (1 if on else 0, user_id))
        await db.commit()


async def upsert_score(user_id: int, name: str, coins: int, level: int, wins: int,
                       best_easy: Optional[int], best_medium: Optional[int], best_hard: Optional[int]) -> int:
    """Сохранить/обновить результат. Возвращает место игрока в рейтинге."""
    def _best(new, old):
        vals = [v for v in (new, old) if v is not None]
        return min(vals) if vals else None

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT best_easy, best_medium, best_hard FROM scores WHERE user_id=?", (user_id,)
        )
        prev = await cur.fetchone()
        if prev:
            be = _best(best_easy, prev[0]); bm = _best(best_medium, prev[1]); bh = _best(best_hard, prev[2])
            await db.execute(
                """UPDATE scores SET name=?, coins=?, level=?, wins=?,
                   best_easy=?, best_medium=?, best_hard=?, updated_at=? WHERE user_id=?""",
                (name, coins, level, wins, be, bm, bh, _now(), user_id),
            )
        else:
            await db.execute(
                """INSERT INTO scores(user_id, name, coins, level, wins,
                   best_easy, best_medium, best_hard, updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (user_id, name, coins, level, wins, best_easy, best_medium, best_hard, _now()),
            )
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM scores WHERE coins > ?", (coins,))
        rank = (await cur.fetchone())[0] + 1
        return rank


async def top_scores(limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT name, coins, level, wins FROM scores ORDER BY coins DESC, level DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [{"name": r[0], "coins": r[1], "level": r[2], "wins": r[3]} for r in rows]


async def total_players() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM scores")
        return (await cur.fetchone())[0]


async def inactive_users(hours: int = 22, limit: int = 25):
    """Игроки, которых стоит подтолкнуть: неактивны > hours и давно не напоминали, напоминания включены."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT user_id FROM users
               WHERE reminders=1
                 AND (last_active IS NULL OR last_active < ?)
                 AND (reminded_at IS NULL OR reminded_at < ?)
               LIMIT ?""",
            (cutoff, cutoff, limit),
        )
        return [r[0] for r in await cur.fetchall()]


async def mark_reminded(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reminded_at=? WHERE user_id=?", (_now(), user_id))
        await db.commit()

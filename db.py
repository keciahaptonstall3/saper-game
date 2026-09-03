"""
Слой хранилища. Два движка:
  • Postgres (asyncpg) — если задан DATABASE_URL (так на Render: данные не теряются).
  • SQLite (aiosqlite) — запасной вариант для локального запуска без базы.

Одинаковые функции для обоих; выбор — по наличию DATABASE_URL.
"""

import datetime as dt
import os
from typing import Optional

DATABASE_URL = os.getenv("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import asyncpg
    _pool: "asyncpg.Pool | None" = None
else:
    import aiosqlite
    DB_PATH = os.getenv("SQLITE_PATH", "saper.db")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _best(new, old):
    vals = [v for v in (new, old) if v is not None]
    return min(vals) if vals else None


# ============================ init ============================

async def init_db() -> None:
    if USE_PG:
        global _pool
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as c:
            await c.execute("""CREATE TABLE IF NOT EXISTS users(
                user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
                referred_by BIGINT, referrals INTEGER DEFAULT 0,
                joined_at TEXT, last_active TEXT, reminded_at TEXT, reminders INTEGER DEFAULT 1)""")
            await c.execute("""CREATE TABLE IF NOT EXISTS scores(
                user_id BIGINT PRIMARY KEY, name TEXT, coins INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
                wins INTEGER DEFAULT 0, best_easy INTEGER, best_medium INTEGER, best_hard INTEGER, updated_at TEXT)""")
            await c.execute("""CREATE TABLE IF NOT EXISTS payments(
                charge_id TEXT PRIMARY KEY, user_id BIGINT, coins INTEGER, stars INTEGER,
                claimed INTEGER DEFAULT 0, created_at TEXT)""")
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                    referred_by INTEGER, referrals INTEGER DEFAULT 0,
                    joined_at TEXT, last_active TEXT, reminded_at TEXT, reminders INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS scores(
                    user_id INTEGER PRIMARY KEY, name TEXT, coins INTEGER DEFAULT 0, level INTEGER DEFAULT 1,
                    wins INTEGER DEFAULT 0, best_easy INTEGER, best_medium INTEGER, best_hard INTEGER, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS payments(
                    charge_id TEXT PRIMARY KEY, user_id INTEGER, coins INTEGER, stars INTEGER,
                    claimed INTEGER DEFAULT 0, created_at TEXT);
            """)
            await db.commit()


# ============================ users / referrals ============================

async def add_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> bool:
    if USE_PG:
        async with _pool.acquire() as c:
            exists = await c.fetchrow("SELECT 1 FROM users WHERE user_id=$1", user_id)
            if exists:
                await c.execute("UPDATE users SET username=$1, first_name=$2, last_active=$3 WHERE user_id=$4",
                                username, first_name, _now(), user_id)
                return False
            await c.execute("INSERT INTO users(user_id, username, first_name, joined_at, last_active) VALUES($1,$2,$3,$4,$5)",
                            user_id, username, first_name, _now(), _now())
            return True
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        if await cur.fetchone():
            await db.execute("UPDATE users SET username=?, first_name=?, last_active=? WHERE user_id=?",
                             (username, first_name, _now(), user_id))
            await db.commit()
            return False
        await db.execute("INSERT INTO users(user_id, username, first_name, joined_at, last_active) VALUES(?,?,?,?,?)",
                         (user_id, username, first_name, _now(), _now()))
        await db.commit()
        return True


async def set_referral(user_id: int, referrer_id: int) -> bool:
    if user_id == referrer_id:
        return False
    if USE_PG:
        async with _pool.acquire() as c:
            row = await c.fetchrow("SELECT referred_by FROM users WHERE user_id=$1", user_id)
            if row is None or row["referred_by"] is not None:
                return False
            if await c.fetchrow("SELECT 1 FROM users WHERE user_id=$1", referrer_id) is None:
                return False
            await c.execute("UPDATE users SET referred_by=$1 WHERE user_id=$2", referrer_id, user_id)
            await c.execute("UPDATE users SET referrals=referrals+1 WHERE user_id=$1", referrer_id)
            return True
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row is None or row[0] is not None:
            return False
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,))
        if await cur.fetchone() is None:
            return False
        await db.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, user_id))
        await db.execute("UPDATE users SET referrals=referrals+1 WHERE user_id=?", (referrer_id,))
        await db.commit()
        return True


async def referrals_count(user_id: int) -> int:
    if USE_PG:
        async with _pool.acquire() as c:
            val = await c.fetchval("SELECT referrals FROM users WHERE user_id=$1", user_id)
            return int(val) if val is not None else 0
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referrals FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def touch_active(user_id: int) -> None:
    if USE_PG:
        async with _pool.acquire() as c:
            await c.execute("UPDATE users SET last_active=$1 WHERE user_id=$2", _now(), user_id)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_active=? WHERE user_id=?", (_now(), user_id))
        await db.commit()


async def set_reminders(user_id: int, on: bool) -> None:
    v = 1 if on else 0
    if USE_PG:
        async with _pool.acquire() as c:
            await c.execute("UPDATE users SET reminders=$1 WHERE user_id=$2", v, user_id)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reminders=? WHERE user_id=?", (v, user_id))
        await db.commit()


# ============================ scores / leaderboard ============================

async def upsert_score(user_id: int, name: str, coins: int, level: int, wins: int,
                       best_easy, best_medium, best_hard) -> int:
    if USE_PG:
        async with _pool.acquire() as c:
            prev = await c.fetchrow("SELECT best_easy, best_medium, best_hard FROM scores WHERE user_id=$1", user_id)
            if prev:
                be = _best(best_easy, prev["best_easy"]); bm = _best(best_medium, prev["best_medium"]); bh = _best(best_hard, prev["best_hard"])
                await c.execute("""UPDATE scores SET name=$1, coins=$2, level=$3, wins=$4,
                    best_easy=$5, best_medium=$6, best_hard=$7, updated_at=$8 WHERE user_id=$9""",
                    name, coins, level, wins, be, bm, bh, _now(), user_id)
            else:
                await c.execute("""INSERT INTO scores(user_id, name, coins, level, wins,
                    best_easy, best_medium, best_hard, updated_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    user_id, name, coins, level, wins, best_easy, best_medium, best_hard, _now())
            rank = await c.fetchval("SELECT COUNT(*) FROM scores WHERE coins > $1", coins)
            return int(rank) + 1
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT best_easy, best_medium, best_hard FROM scores WHERE user_id=?", (user_id,))
        prev = await cur.fetchone()
        if prev:
            be = _best(best_easy, prev[0]); bm = _best(best_medium, prev[1]); bh = _best(best_hard, prev[2])
            await db.execute("""UPDATE scores SET name=?, coins=?, level=?, wins=?,
                best_easy=?, best_medium=?, best_hard=?, updated_at=? WHERE user_id=?""",
                (name, coins, level, wins, be, bm, bh, _now(), user_id))
        else:
            await db.execute("""INSERT INTO scores(user_id, name, coins, level, wins,
                best_easy, best_medium, best_hard, updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (user_id, name, coins, level, wins, best_easy, best_medium, best_hard, _now()))
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM scores WHERE coins > ?", (coins,))
        return (await cur.fetchone())[0] + 1


async def top_scores(limit: int = 20):
    if USE_PG:
        async with _pool.acquire() as c:
            rows = await c.fetch("SELECT name, coins, level, wins FROM scores ORDER BY coins DESC, level DESC LIMIT $1", limit)
            return [{"name": r["name"], "coins": r["coins"], "level": r["level"], "wins": r["wins"]} for r in rows]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name, coins, level, wins FROM scores ORDER BY coins DESC, level DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [{"name": r[0], "coins": r[1], "level": r[2], "wins": r[3]} for r in rows]


async def total_players() -> int:
    if USE_PG:
        async with _pool.acquire() as c:
            return int(await c.fetchval("SELECT COUNT(*) FROM scores"))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM scores")
        return (await cur.fetchone())[0]


# ============================ reminders ============================

async def inactive_users(hours: int = 22, limit: int = 25):
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
    if USE_PG:
        async with _pool.acquire() as c:
            rows = await c.fetch("""SELECT user_id FROM users
                WHERE reminders=1 AND (last_active IS NULL OR last_active < $1)
                  AND (reminded_at IS NULL OR reminded_at < $2) LIMIT $3""", cutoff, cutoff, limit)
            return [r["user_id"] for r in rows]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""SELECT user_id FROM users
            WHERE reminders=1 AND (last_active IS NULL OR last_active < ?)
              AND (reminded_at IS NULL OR reminded_at < ?) LIMIT ?""", (cutoff, cutoff, limit))
        return [r[0] for r in await cur.fetchall()]


async def mark_reminded(user_id: int) -> None:
    if USE_PG:
        async with _pool.acquire() as c:
            await c.execute("UPDATE users SET reminded_at=$1 WHERE user_id=$2", _now(), user_id)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET reminded_at=? WHERE user_id=?", (_now(), user_id))
        await db.commit()


# ============================ payments (Telegram Stars) ============================

async def add_payment(charge_id: str, user_id: int, coins: int, stars: int) -> bool:
    """Записать оплату (идемпотентно по charge_id). True — если это новая оплата."""
    if USE_PG:
        async with _pool.acquire() as c:
            status = await c.execute(
                """INSERT INTO payments(charge_id, user_id, coins, stars, created_at)
                   VALUES($1,$2,$3,$4,$5) ON CONFLICT (charge_id) DO NOTHING""",
                charge_id, user_id, coins, stars, _now())
            return status.split()[-1] == "1"  # 'INSERT 0 1' -> новая; 'INSERT 0 0' -> дубль
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO payments(charge_id, user_id, coins, stars, created_at) VALUES(?,?,?,?,?)",
                             (charge_id, user_id, coins, stars, _now()))
        except aiosqlite.IntegrityError:
            return False
        await db.commit()
        return True


async def claim_payments(user_id: int) -> int:
    """Отдать сумму неполученных купленных монет и пометить их полученными."""
    if USE_PG:
        async with _pool.acquire() as c:
            async with c.transaction():
                total = await c.fetchval(
                    "SELECT COALESCE(SUM(coins),0) FROM payments WHERE user_id=$1 AND claimed=0", user_id)
                if total:
                    await c.execute("UPDATE payments SET claimed=1 WHERE user_id=$1 AND claimed=0", user_id)
            return int(total or 0)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(SUM(coins),0) FROM payments WHERE user_id=? AND claimed=0", (user_id,))
        total = (await cur.fetchone())[0] or 0
        if total:
            await db.execute("UPDATE payments SET claimed=1 WHERE user_id=? AND claimed=0", (user_id,))
            await db.commit()
        return int(total)

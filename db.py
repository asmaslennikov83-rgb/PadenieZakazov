from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import aiosqlite

from config import DB_PATH, ALLOWED_TELEGRAM_IDS


DEFAULT_THRESHOLD = 15
DEFAULT_SORT = "percent"
DEFAULT_TIME = "10:00"
DEFAULT_CABINET = "both"


@dataclass
class UserSettings:
    user_id: int
    threshold: int = DEFAULT_THRESHOLD
    sort_mode: str = DEFAULT_SORT
    report_time: str = DEFAULT_TIME
    cabinet_mode: str = DEFAULT_CABINET
    last_sent_date: str | None = None


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                threshold INTEGER NOT NULL DEFAULT 15,
                sort_mode TEXT NOT NULL DEFAULT 'percent',
                report_time TEXT NOT NULL DEFAULT '10:00',
                cabinet_mode TEXT NOT NULL DEFAULT 'both',
                last_sent_date TEXT
            )
        """)
        for user_id in ALLOWED_TELEGRAM_IDS:
            await db.execute(
                "INSERT OR IGNORE INTO user_settings(user_id) VALUES (?)",
                (user_id,),
            )
        await db.commit()


async def ensure_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_settings(user_id) VALUES (?)",
            (user_id,),
        )
        await db.commit()


async def get_user_settings(user_id: int) -> UserSettings:
    await ensure_user(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
    return UserSettings(**dict(row))


async def list_user_settings() -> list[UserSettings]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM user_settings")
        rows = await cur.fetchall()
    return [UserSettings(**dict(row)) for row in rows]


async def update_setting(user_id: int, field: str, value) -> None:
    allowed = {"threshold", "sort_mode", "report_time", "cabinet_mode", "last_sent_date"}
    if field not in allowed:
        raise ValueError("Недопустимое поле настройки")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE user_settings SET {field} = ? WHERE user_id = ?",
            (value, user_id),
        )
        await db.commit()


async def mark_sent(user_id: int, day: date) -> None:
    await update_setting(user_id, "last_sent_date", day.isoformat())

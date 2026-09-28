from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _csv_ints(value: str) -> set[int]:
    result: set[int] = set()
    for item in (value or "").split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


@dataclass(frozen=True)
class Cabinet:
    code: str
    name: str
    token: str
    analytics_token: str = ""


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_TELEGRAM_IDS = _csv_ints(os.getenv("ALLOWED_TELEGRAM_IDS", ""))

CABINETS = {
    "spb_tt": Cabinet(
        code="spb_tt",
        name=os.getenv("WB_NAME_SPB_TT", 'ООО «СПб-ТТ»'),
        token=os.getenv("WB_TOKEN_SPB_TT", "").strip(),
        analytics_token=os.getenv("WB_ANALYTICS_TOKEN_SPB_TT", "").strip(),
    ),
    "spb_tt_group": Cabinet(
        code="spb_tt_group",
        name=os.getenv("WB_NAME_SPB_TT_GROUP", 'ООО «СПБ-ТТ ГРУПП»'),
        token=os.getenv("WB_TOKEN_SPB_TT_GROUP", "").strip(),
        analytics_token=os.getenv("WB_ANALYTICS_TOKEN_SPB_TT_GROUP", "").strip(),
    ),
}

DB_PATH = Path(os.getenv("DB_PATH", "data/bot.db"))
REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "Europe/Moscow")
WB_CACHE_MINUTES = int(os.getenv("WB_CACHE_MINUTES", "20"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "60"))
WB_429_FALLBACK_SECONDS = int(os.getenv("WB_429_FALLBACK_SECONDS", "65"))


def validate_config() -> None:
    missing: list[str] = []
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not ALLOWED_TELEGRAM_IDS:
        missing.append("ALLOWED_TELEGRAM_IDS")
    for cabinet in CABINETS.values():
        if not cabinet.token:
            missing.append(f"WB token for {cabinet.code}")
    if missing:
        raise RuntimeError("Не заполнены обязательные настройки: " + ", ".join(missing))

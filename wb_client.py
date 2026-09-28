from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from config import (
    CABINETS,
    HTTP_TIMEOUT_SECONDS,
    WB_429_FALLBACK_SECONDS,
    WB_CACHE_MINUTES,
)

MSK = ZoneInfo("Europe/Moscow")
BASE_URL = "https://statistics-api.wildberries.ru"
ORDERS_PATH = "/api/v1/supplier/orders"


class WBApiError(RuntimeError):
    pass


@dataclass
class CacheEntry:
    created_monotonic: float
    start_date: date
    end_date: date
    rows: list[dict[str, Any]]


class WBOrdersClient:
    """
    Statistics API.

    Важно:
    - flag=0 используется намеренно.
    - dateFrom относится к lastChangeDate, а не к дате заказа.
    - После получения данных дополнительно фильтруем строки по полю `date`.
    - В результатах считаем каждую строку как 1 заказанную единицу.
    - isCancel НЕ фильтруем: по согласованной логике отменённые заказы тоже входят.
    """

    def __init__(self) -> None:
        self._cache: dict[str, CacheEntry] = {}
        self._locks = {code: asyncio.Lock() for code in CABINETS}

    async def get_period_orders(
        self,
        cabinet_code: str,
        start_date: date,
        end_date: date,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        if cabinet_code not in CABINETS:
            raise ValueError(f"Неизвестный кабинет: {cabinet_code}")

        async with self._locks[cabinet_code]:
            cached = self._cache.get(cabinet_code)
            if (
                not force_refresh
                and cached
                and cached.start_date == start_date
                and cached.end_date == end_date
                and time.monotonic() - cached.created_monotonic < WB_CACHE_MINUTES * 60
            ):
                return list(cached.rows)

            cabinet = CABINETS[cabinet_code]

            # Берём небольшой запас по lastChangeDate.
            # Для ежедневного мониторинга основной расчёт всё равно фильтруется по `date`.
            date_from = datetime.combine(
                start_date - timedelta(days=2),
                datetime.min.time(),
                tzinfo=MSK,
            )

            rows = await self._fetch_changed_since(cabinet.token, date_from)

            filtered: list[dict[str, Any]] = []
            for row in rows:
                order_date = parse_wb_order_date(row.get("date"))
                if order_date is None:
                    continue
                d = order_date.date()
                if start_date <= d <= end_date:
                    filtered.append(row)

            self._cache[cabinet_code] = CacheEntry(
                created_monotonic=time.monotonic(),
                start_date=start_date,
                end_date=end_date,
                rows=filtered,
            )
            return list(filtered)

    async def _fetch_changed_since(
        self,
        token: str,
        date_from: datetime,
    ) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        headers = {"Authorization": token}
        result: list[dict[str, Any]] = []

        cursor = date_from
        seen_cursor: str | None = None

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            while True:
                params = {
                    "dateFrom": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                    "flag": 0,
                }
                page = await self._request_json(session, params)

                if not isinstance(page, list):
                    raise WBApiError("WB вернул неожиданный формат ответа по заказам")

                if not page:
                    break

                result.extend(page)

                # Документация указывает условный лимит ~80 000 строк на ответ.
                # Если ответ явно меньше, дополнительные запросы не нужны.
                if len(page) < 79_000:
                    break

                last_change = page[-1].get("lastChangeDate")
                if not last_change or last_change == seen_cursor:
                    break

                seen_cursor = last_change
                parsed = parse_wb_datetime(last_change)
                if parsed is None:
                    break

                cursor = parsed

                # Statistics API имеет низкий лимит запросов.
                await asyncio.sleep(61)

        return result

    async def _request_json(
        self,
        session: aiohttp.ClientSession,
        params: dict[str, Any],
    ) -> Any:
        attempts = 4
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                async with session.get(BASE_URL + ORDERS_PATH, params=params) as response:
                    if response.status == 200:
                        return await response.json(content_type=None)

                    body = (await response.text())[:700]

                    if response.status == 429:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = int(float(retry_after)) if retry_after else WB_429_FALLBACK_SECONDS
                        except ValueError:
                            delay = WB_429_FALLBACK_SECONDS
                        if attempt < attempts:
                            await asyncio.sleep(max(delay, 1))
                            continue

                    if response.status in {500, 502, 503, 504} and attempt < attempts:
                        await asyncio.sleep(min(2 ** attempt, 15))
                        continue

                    raise WBApiError(
                        f"WB API: HTTP {response.status}. Ответ: {body}"
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(min(2 ** attempt, 15))
                    continue
                break

        raise WBApiError(f"Ошибка соединения с WB API: {last_error}")


def parse_wb_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def parse_wb_order_date(value: Any) -> datetime | None:
    return parse_wb_datetime(value)


wb_orders_client = WBOrdersClient()

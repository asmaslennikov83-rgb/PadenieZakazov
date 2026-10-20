from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
import aiohttp
from config import CABINETS, HTTP_TIMEOUT_SECONDS, WB_429_FALLBACK_SECONDS
from data_quality import nm_id, count, number

logger = logging.getLogger(__name__)
BASE_URL = "https://seller-analytics-api.wildberries.ru"
FUNNEL_PERIOD_PATH = "/api/analytics/v3/sales-funnel/products"
SEARCH_DETAILS_PATH = "/api/v2/search-report/table/details"


class AnalyticsError(RuntimeError):
    pass


@dataclass
class FunnelMetrics:
    views: int | None = None
    carts: int | None = None
    orders: int | None = None
    wishlist: int | None = None


@dataclass
class SearchMetrics:
    avg_position: float | None = None
    avg_position_past: float | None = None


def products(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise AnalyticsError("Ожидался объект data.products")
    result = data["data"].get("products")
    if not isinstance(result, list) or any(not isinstance(x, dict) for x in result):
        raise AnalyticsError("Ожидался массив data.products")
    return result


def metric(obj, start, end):
    if not isinstance(obj, dict):
        return None
    period = obj.get("period")
    if not isinstance(period, dict) or period.get("start") != start.isoformat() or period.get("end") != end.isoformat():
        raise AnalyticsError("WB вернул другой или неуказанный период воронки")
    return FunnelMetrics(count(obj.get("openCount")), count(obj.get("cartCount")),
                         count(obj.get("orderCount")), count(obj.get("addToWishlist")))


class AnalyticsClient:
    def __init__(self):
        self._locks = {}
        self._last = {}

    async def _pace(self, cabinet, path):
        key = (cabinet, path)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await asyncio.sleep(max(0, 21 - (time.monotonic() - self._last.get(key, 0))))
            self._last[key] = time.monotonic()

    def _token(self, cabinet_code):
        cabinet = CABINETS[cabinet_code]
        return cabinet.analytics_token or cabinet.token

    async def get_funnel_periods(self, cabinet_code, first_start, first_end, last_start, last_end, nm_ids):
        if not self._token(cabinet_code):
            raise AnalyticsError("Не задан токен Аналитики")
        unique = sorted({n for x in nm_ids if (n := nm_id(x)) is not None})
        first, last = {}, {}
        for begin in range(0, len(unique), 1000):
            chunk = unique[begin:begin + 1000]
            offset = 0
            seen = set()
            while True:
                payload = {
                    "selectedPeriod": {"start": last_start.isoformat(), "end": last_end.isoformat()},
                    "pastPeriod": {"start": first_start.isoformat(), "end": first_end.isoformat()},
                    "nmIds": chunk, "skipDeletedNm": False,
                    "limit": 1000, "offset": offset,
                }
                page = products(await self._post_json(cabinet_code, FUNNEL_PERIOD_PATH, payload))
                for item in page:
                    product = item.get("product")
                    n = nm_id(product.get("nmId")) if isinstance(product, dict) else None
                    if n is None or n not in chunk or n in seen:
                        raise AnalyticsError("Некорректный/повторный nmId в воронке или пагинации")
                    seen.add(n)
                    stats = item.get("statistic")
                    if not isinstance(stats, dict):
                        raise AnalyticsError("Отсутствует statistic в ответе воронки")
                    past = metric(stats.get("past"), first_start, first_end)
                    selected = metric(stats.get("selected"), last_start, last_end)
                    if past is not None:
                        first[n] = past
                    if selected is not None:
                        last[n] = selected
                if len(page) < 1000:
                    break
                offset += len(page)
        return first, last

    async def _search_period(self, cabinet_code, start, end, nm_ids):
        result = {}
        for begin in range(0, len(nm_ids), 50):
            chunk = nm_ids[begin:begin + 50]
            offset, seen = 0, set()
            while True:
                payload = {
                    "currentPeriod": {"start": start.isoformat(), "end": end.isoformat()},
                    "nmIds": chunk, "orderBy": {"field": "avgPosition", "mode": "asc"},
                    "positionCluster": "all", "includeSubstitutedSKUs": True,
                    "includeSearchTexts": True, "limit": 1000, "offset": offset,
                }
                page = products(await self._post_json(cabinet_code, SEARCH_DETAILS_PATH, payload, optional_access=True))
                for obj in page:
                    n = nm_id(obj.get("nmId"))
                    if n is None or n not in chunk or n in seen:
                        raise AnalyticsError("Некорректный/повторный nmId в поисковой аналитике")
                    seen.add(n)
                    pos = obj.get("avgPosition")
                    value = number(pos.get("current")) if isinstance(pos, dict) else None
                    if value is not None and value > 0:
                        result[n] = value
                if len(page) < 1000:
                    break
                offset += len(page)
        return result

    async def get_search_positions(self, cabinet_code, current_start, current_end, past_start, past_end, nm_ids):
        if not self._token(cabinet_code):
            raise AnalyticsError("Не задан токен Аналитики")
        ids = sorted({n for x in nm_ids if (n := nm_id(x)) is not None})
        current = await self._search_period(cabinet_code, current_start, current_end, ids)
        past = await self._search_period(cabinet_code, past_start, past_end, ids)
        return {n: SearchMetrics(current.get(n), past.get(n)) for n in current.keys() | past.keys()}

    async def _post_json(self, cabinet_code: str, path: str, payload: dict[str, Any], optional_access: bool = False) -> Any:
        await self._pace(cabinet_code, path)
        token = self._token(cabinet_code)
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        headers = {"Authorization": token, "Content-Type": "application/json"}
        last_error: Exception | None = None
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for attempt in range(1, 4):
                try:
                    async with session.post(BASE_URL + path, json=payload) as response:
                        text = await response.text()
                        if response.status == 200:
                            return await response.json(content_type=None)
                        if optional_access and response.status in {402, 403, 404}:
                            logger.info("Опциональная аналитика недоступна %s: HTTP %s", path, response.status)
                            raise AnalyticsError(f"Метод недоступен {path}: HTTP {response.status}")
                        if response.status == 429 and attempt < 3:
                            retry_after = response.headers.get("Retry-After")
                            try:
                                delay = int(float(retry_after)) if retry_after else WB_429_FALLBACK_SECONDS
                            except ValueError:
                                delay = WB_429_FALLBACK_SECONDS
                            await asyncio.sleep(max(20, delay))
                            continue
                        if response.status in {500, 502, 503, 504} and attempt < 3:
                            await asyncio.sleep(3 * attempt)
                            continue
                        raise AnalyticsError(f"WB Analytics {path}: HTTP {response.status}")
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_error = exc
                    if attempt < 3:
                        await asyncio.sleep(3 * attempt)
                        continue
        raise AnalyticsError(f"Ошибка соединения с WB Analytics: {last_error}")


analytics_client = AnalyticsClient()

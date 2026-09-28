from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiohttp

from config import CABINETS, HTTP_TIMEOUT_SECONDS, WB_429_FALLBACK_SECONDS

logger = logging.getLogger(__name__)
BASE_URL = "https://seller-analytics-api.wildberries.ru"
FUNNEL_HISTORY_PATH = "/api/analytics/v3/sales-funnel/products/history"
SEARCH_DETAILS_PATH = "/api/v2/search-report/table/details"


class AnalyticsError(RuntimeError):
    pass


@dataclass
class FunnelMetrics:
    views: int = 0
    carts: int = 0
    orders: int = 0
    wishlist: int = 0


@dataclass
class SearchMetrics:
    avg_position: float | None = None
    avg_position_past: float | None = None


class AnalyticsClient:
    def _token(self, cabinet_code: str) -> str:
        cabinet = CABINETS[cabinet_code]
        return cabinet.analytics_token or cabinet.token

    async def get_funnel_history(self, cabinet_code: str, start: date, end: date, nm_ids: list[int]) -> dict[int, FunnelMetrics]:
        token = self._token(cabinet_code)
        if not token or not nm_ids:
            return {}
        result: dict[int, FunnelMetrics] = {}
        unique_ids = sorted({int(x) for x in nm_ids if x})
        for chunk_start in range(0, len(unique_ids), 1000):
            chunk = unique_ids[chunk_start:chunk_start + 1000]
            payload = {
                "selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
                "nmIds": chunk,
                "aggregationLevel": "day",
                "skipDeletedNm": False,
            }
            data = await self._post_json(cabinet_code, FUNNEL_HISTORY_PATH, payload)
            items = data.get("data", data.get("result", data)) if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise AnalyticsError("WB воронка: неожиданный формат ответа")
            for item in items:
                if not isinstance(item, dict):
                    continue
                product = item.get("product") or {}
                nm_raw = product.get("nmId") or product.get("nmID") or item.get("nmId") or item.get("nmID")
                try:
                    nm_id = int(nm_raw)
                except (TypeError, ValueError):
                    continue
                metric = result.setdefault(nm_id, FunnelMetrics())
                history = item.get("history") or item.get("data") or []
                if not isinstance(history, list):
                    continue
                for day in history:
                    if not isinstance(day, dict):
                        continue
                    metric.views += _int_value(day, "openCount", "openCard")
                    metric.carts += _int_value(day, "cartCount", "addToCart")
                    metric.orders += _int_value(day, "orderCount", "ordersCount")
                    metric.wishlist += _int_value(day, "addToWishlistCount", "addToWishlist")
            if chunk_start + 1000 < len(unique_ids):
                await asyncio.sleep(21)
        return result

    async def get_search_positions(
        self, cabinet_code: str,
        current_start: date, current_end: date,
        past_start: date, past_end: date,
        nm_ids: list[int],
    ) -> dict[int, SearchMetrics]:
        token = self._token(cabinet_code)
        if not token or not nm_ids:
            return {}
        result: dict[int, SearchMetrics] = {}
        unique_ids = sorted({int(x) for x in nm_ids if x})
        for chunk_start in range(0, len(unique_ids), 50):
            chunk = unique_ids[chunk_start:chunk_start + 50]
            payload = {
                "currentPeriod": {"start": current_start.isoformat(), "end": current_end.isoformat()},
                "pastPeriod": {"start": past_start.isoformat(), "end": past_end.isoformat()},
                "nmIds": chunk,
                "orderBy": {"field": "avgPosition", "mode": "asc"},
                "positionCluster": "all",
                "includeSubstitutedSKUs": True,
                "includeSearchTexts": True,
                "limit": 1000,
                "offset": 0,
            }
            data = await self._post_json(cabinet_code, SEARCH_DETAILS_PATH, payload, optional_access=True)
            if not data:
                return result
            for obj in _walk_dicts(data):
                nm_raw = obj.get("nmId", obj.get("nmID"))
                try:
                    nm_id = int(nm_raw)
                except (TypeError, ValueError):
                    continue
                if nm_id not in chunk:
                    continue
                current_pos = _extract_position(obj, True)
                past_pos = _extract_position(obj, False)
                if current_pos is not None or past_pos is not None:
                    m = result.setdefault(nm_id, SearchMetrics())
                    if current_pos is not None:
                        m.avg_position = current_pos
                    if past_pos is not None:
                        m.avg_position_past = past_pos
            if chunk_start + 50 < len(unique_ids):
                await asyncio.sleep(21)
        return result

    async def _post_json(self, cabinet_code: str, path: str, payload: dict[str, Any], optional_access: bool = False) -> Any:
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
                            return {}
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
                        raise AnalyticsError(f"WB Analytics {path}: HTTP {response.status}: {text[:500]}")
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_error = exc
                    if attempt < 3:
                        await asyncio.sleep(3 * attempt)
                        continue
        raise AnalyticsError(f"Ошибка соединения с WB Analytics: {last_error}")


def _int_value(obj: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in obj:
            try:
                return int(round(float(obj.get(key) or 0)))
            except (TypeError, ValueError):
                pass
    return 0


def _float_value(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _extract_position(obj: dict[str, Any], current: bool) -> float | None:
    keys = ("avgPosition", "averagePosition")
    if current:
        for key in keys:
            value = _float_value(obj.get(key))
            if value is not None:
                return value
        containers = ("current", "selected", "currentPeriod", "selectedPeriod")
    else:
        containers = ("past", "previous", "pastPeriod", "previousPeriod")
    for container_key in containers:
        container = obj.get(container_key)
        if isinstance(container, dict):
            for key in keys:
                value = _float_value(container.get(key))
                if value is not None:
                    return value
    return None


analytics_client = AnalyticsClient()

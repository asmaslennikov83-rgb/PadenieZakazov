from __future__ import annotations

import asyncio
import csv
import io
import logging
import json
import time
import uuid
import zipfile
from datetime import date, datetime
from typing import Any

import aiohttp

from config import CABINETS, HTTP_TIMEOUT_SECONDS
from data_quality import nm_id, count

logger = logging.getLogger(__name__)

BASE_URL = "https://seller-analytics-api.wildberries.ru"
CREATE_PATH = "/api/v2/nm-report/downloads"
STATUS_PATH = "/api/v2/nm-report/downloads"
DOWNLOAD_PATH = "/api/v2/nm-report/downloads/file/{download_id}"


class StockHistoryError(RuntimeError):
    pass


class StockHistoryClient:
    """Получение дневных исторических остатков WB через STOCK_HISTORY_DAILY_CSV."""

    def __init__(self):
        self._cache = {}
        self._locks = {}
        self._last_create = {}

    async def get_stock_for_day(self, cabinet_code, day, nm_ids, stock_type):
        ids = tuple(sorted({n for value in nm_ids if (n := nm_id(value)) is not None}))
        key = (cabinet_code, day, ids, stock_type)
        lock = self._locks.setdefault(cabinet_code, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < 7200:
                return dict(cached[1])
            await asyncio.sleep(max(0, 21 - (time.monotonic() - self._last_create.get(cabinet_code, 0))))
            self._last_create[cabinet_code] = time.monotonic()
            value = await self._get_stock_for_day(cabinet_code, day, list(ids), stock_type)
            self._cache[key] = (time.monotonic(), value)
            return dict(value)

    async def _get_stock_for_day(
        self,
        cabinet_code: str,
        day: date,
        nm_ids: list[int],
        stock_type: str,
    ) -> dict[int, int]:
        if stock_type not in {"wb", "mp"}:
            raise ValueError("stock_type должен быть wb или mp")
        cabinet = CABINETS[cabinet_code]
        token = getattr(cabinet, "analytics_token", "") or cabinet.token
        if not token:
            raise StockHistoryError("Не задан токен Аналитики")

        nm_ids = sorted({int(x) for x in nm_ids if x})
        if not nm_ids:
            return {}

        download_id = str(uuid.uuid4())
        payload = {
            "id": download_id,
            "reportType": "STOCK_HISTORY_DAILY_CSV",
            "userReportName": f"WB stock {stock_type} {day.isoformat()}",
            "params": {
                "currentPeriod": {
                    "start": day.isoformat(),
                    "end": day.isoformat(),
                },
                "stockType": stock_type,
                "skipDeletedNm": False,
                "nmIds": nm_ids,
            },
        }

        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        headers = {"Authorization": token, "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            await self._create(session, payload)
            await self._wait_ready(session, download_id)
            archive = await self._download(session, download_id)

        return {n: q for n, q in self._parse_archive(archive, day).items() if n in nm_ids}

    async def _request(self, session, method, path, **kwargs):
        for attempt in range(3):
            try:
                async with session.request(method, BASE_URL + path, **kwargs) as response:
                    body = await response.read()
                    if response.status in {200, 201, 202}:
                        return body
                    if response.status == 429 and attempt < 2:
                        try:
                            delay = float(response.headers.get("Retry-After", "65"))
                        except ValueError:
                            delay = 65
                        await asyncio.sleep(max(21, delay))
                        continue
                    if response.status >= 500 and attempt < 2:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    raise StockHistoryError(f"История остатков {path}: HTTP {response.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == 2:
                    raise StockHistoryError("Нет соединения с WB для истории остатков") from exc
                await asyncio.sleep(3 * (attempt + 1))

    async def _create(self, session: aiohttp.ClientSession, payload: dict[str, Any]) -> None:
        await self._request(session, "POST", CREATE_PATH, json=payload)

    async def _wait_ready(self, session: aiohttp.ClientSession, download_id: str) -> None:
        # Лимит WB: 3 запроса/мин, интервал 20 сек.
        # Первый статус проверяем через 20 сек и далее не чаще этого интервала.
        for _ in range(7):
            await asyncio.sleep(20)
            params = [("filter[downloadIds]", download_id)]
            body = await self._request(session, "GET", STATUS_PATH, params=params)
            try:
                data = json.loads(body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise StockHistoryError("WB вернул некорректный статус отчёта") from exc

            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
            item = next((x for x in items if isinstance(x, dict) and str(x.get("id")) == download_id), None)
            if not item:
                continue
            status = str(item.get("status", "")).upper()
            if status == "SUCCESS":
                return
            if status == "FAILED":
                raise StockHistoryError("WB не смог сформировать отчёт истории остатков")

        raise StockHistoryError("История остатков WB не успела сформироваться")

    async def _download(self, session: aiohttp.ClientSession, download_id: str) -> bytes:
        return await self._request(session, "GET", DOWNLOAD_PATH.format(download_id=download_id))

    def _parse_archive(self, archive: bytes, day: date) -> dict[int, int]:
        result: dict[int, int] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    raise StockHistoryError("В архиве WB нет CSV")
                parts = [zf.read(name) for name in csv_names]
        except zipfile.BadZipFile as exc:
            raise StockHistoryError("WB вернул повреждённый ZIP истории остатков") from exc

        for raw in parts:
            values = self._parse_csv(raw, day)
            for n, qty in values.items():
                result[n] = result.get(n, 0) + qty
        return result

    def _parse_csv(self, raw, day):
        result = {}
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1251")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise StockHistoryError("Пустой CSV остатков без заголовков")

        # WB менял названия полей в отчётах. Парсер принимает несколько вариантов.
        normalized = {self._norm(h): h for h in reader.fieldnames if h}
        nm_col = self._find_col(normalized, ["nmid", "nmidwb", "wbarticle", "articlewb"])
        # InventoryHistoryReportRes is wide: each date is a quantity column.
        # Example: VendorCode, NmID, ChrtID, OfficeName, 19.10.2026, 20.10.2026.
        day_col = self._find_col(normalized, [day.strftime("%d.%m.%Y"), day.isoformat()])
        qty_col = day_col or self._find_col(
            normalized,
            ["stockcount", "quantity", "stockquantity", "stockqty", "stocks", "stock", "остаток", "остатки"],
        )
        dt_col = None if day_col else self._find_col(normalized, ["dt", "date", "day", "дата"])
        if not nm_col or not qty_col:
            raise StockHistoryError(f"Неизвестные колонки STOCK_HISTORY_DAILY_CSV: {reader.fieldnames}")

        for row in reader:
            if dt_col:
                raw_dt = str(row.get(dt_col, "")).strip()[:10]
                try:
                    parsed_day = date.fromisoformat(raw_dt) if "-" in raw_dt else datetime.strptime(raw_dt, "%d.%m.%Y").date()
                except ValueError as exc:
                    raise StockHistoryError(f"Неизвестный формат даты остатков: {raw_dt}") from exc
                if parsed_day != day:
                    continue
            n = nm_id(row.get(nm_col))
            qty = count(row.get(qty_col))
            if n is None or qty is None:
                raise StockHistoryError("Некорректный nmId или количество в CSV остатков")
            result[n] = result.get(n, 0) + qty

        return result

    @staticmethod
    def _norm(value: str) -> str:
        return "".join(ch.lower() for ch in str(value) if ch.isalnum())

    @staticmethod
    def _find_col(normalized: dict[str, str], candidates: list[str]) -> str | None:
        for candidate in candidates:
            key = StockHistoryClient._norm(candidate)
            if key in normalized:
                return normalized[key]
        return None


stock_history_client = StockHistoryClient()

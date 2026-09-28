from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
import zipfile
from datetime import date
from typing import Any

import aiohttp

from config import CABINETS, HTTP_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

BASE_URL = "https://seller-analytics-api.wildberries.ru"
CREATE_PATH = "/api/v2/nm-report/downloads"
STATUS_PATH = "/api/v2/nm-report/downloads"
DOWNLOAD_PATH = "/api/v2/nm-report/downloads/file/{download_id}"


class StockHistoryError(RuntimeError):
    pass


class StockHistoryClient:
    """Получение дневных исторических остатков WB через STOCK_HISTORY_DAILY_CSV."""

    async def get_stock_for_day(
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
            return {}

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

        return self._parse_archive(archive, day)

    async def _create(self, session: aiohttp.ClientSession, payload: dict[str, Any]) -> None:
        async with session.post(BASE_URL + CREATE_PATH, json=payload) as response:
            text = await response.text()
            if response.status not in {200, 201, 202}:
                raise StockHistoryError(
                    f"Не удалось создать отчёт остатков: HTTP {response.status}: {text[:500]}"
                )

    async def _wait_ready(self, session: aiohttp.ClientSession, download_id: str) -> None:
        # Лимит WB: 3 запроса/мин, интервал 20 сек.
        # Первый статус проверяем через 20 сек и далее не чаще этого интервала.
        for _ in range(7):
            await asyncio.sleep(20)
            params = [("filter[downloadIds]", download_id)]
            async with session.get(BASE_URL + STATUS_PATH, params=params) as response:
                text = await response.text()
                if response.status != 200:
                    if response.status == 429:
                        continue
                    raise StockHistoryError(
                        f"Ошибка статуса отчёта остатков: HTTP {response.status}: {text[:500]}"
                    )
                try:
                    data = await response.json(content_type=None)
                except Exception as exc:
                    raise StockHistoryError("WB вернул некорректный статус отчёта") from exc

            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
            item = next((x for x in items if str(x.get("id")) == download_id), None)
            if not item:
                continue
            status = str(item.get("status", "")).upper()
            if status == "SUCCESS":
                return
            if status == "FAILED":
                raise StockHistoryError("WB не смог сформировать отчёт истории остатков")

        raise StockHistoryError("История остатков WB не успела сформироваться")

    async def _download(self, session: aiohttp.ClientSession, download_id: str) -> bytes:
        url = BASE_URL + DOWNLOAD_PATH.format(download_id=download_id)
        async with session.get(url) as response:
            body = await response.read()
            if response.status != 200:
                raise StockHistoryError(
                    f"Не удалось скачать историю остатков: HTTP {response.status}"
                )
            return body

    def _parse_archive(self, archive: bytes, day: date) -> dict[int, int]:
        result: dict[int, int] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not csv_names:
                    raise StockHistoryError("В архиве WB нет CSV")
                raw = zf.read(csv_names[0])
        except zipfile.BadZipFile as exc:
            raise StockHistoryError("WB вернул повреждённый ZIP истории остатков") from exc

        text = raw.decode("utf-8-sig", errors="replace")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            return result

        # WB менял названия полей в отчётах. Парсер принимает несколько вариантов.
        normalized = {self._norm(h): h for h in reader.fieldnames if h}
        nm_col = self._find_col(normalized, ["nmid", "nmidwb", "wbarticle", "articlewb"])
        qty_col = self._find_col(
            normalized,
            ["stockcount", "quantity", "stockqty", "stocks", "stock", "остаток", "остатки"],
        )
        dt_col = self._find_col(normalized, ["dt", "date", "day", "дата"])
        if not nm_col or not qty_col:
            logger.warning("Неизвестные колонки STOCK_HISTORY_DAILY_CSV: %s", reader.fieldnames)
            return result

        for row in reader:
            if dt_col:
                raw_dt = str(row.get(dt_col, ""))[:10]
                if raw_dt and raw_dt != day.isoformat():
                    continue
            try:
                nm_id = int(float(str(row.get(nm_col, "0")).replace(" ", "")))
                qty_raw = str(row.get(qty_col, "0")).replace(" ", "").replace(",", ".")
                qty = int(round(float(qty_raw or 0)))
            except (TypeError, ValueError):
                continue
            result[nm_id] = result.get(nm_id, 0) + qty

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

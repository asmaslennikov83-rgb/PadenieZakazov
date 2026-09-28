from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from wb_client import wb_orders_client


@dataclass
class DropRow:
    supplier_article: str
    average_per_day: float
    yesterday_orders: int
    drop_percent: float
    absolute_drop: float


@dataclass
class CabinetReport:
    cabinet_code: str
    cabinet_name: str
    baseline_start: date
    baseline_end: date
    yesterday: date
    threshold: int
    rows: list[DropRow]


def _article(row: dict[str, Any]) -> str:
    value = (
        row.get("supplierArticle")
        or row.get("vendorCode")
        or row.get("article")
        or ""
    )
    value = str(value).strip()
    if value:
        return value

    nm_id = row.get("nmId")
    return f"nmID {nm_id}" if nm_id else "Без артикула"


async def build_cabinet_report(
    cabinet_code: str,
    cabinet_name: str,
    threshold: int,
    sort_mode: str,
    today_msk: date,
    force_refresh: bool = False,
) -> CabinetReport:
    yesterday = today_msk - timedelta(days=1)
    baseline_end = yesterday - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=13)

    rows = await wb_orders_client.get_period_orders(
        cabinet_code=cabinet_code,
        start_date=baseline_start,
        end_date=yesterday,
        force_refresh=force_refresh,
    )

    baseline_counts: dict[str, int] = {}
    yesterday_counts: dict[str, int] = {}
    known_articles: set[str] = set()

    for order in rows:
        article = _article(order)
        known_articles.add(article)

        raw_date = order.get("date")
        if not raw_date:
            continue

        # YYYY-MM-DD в начале поля WB достаточно для группировки:
        order_day_str = str(raw_date)[:10]
        try:
            order_day = date.fromisoformat(order_day_str)
        except ValueError:
            continue

        # НЕ фильтруем isCancel — отмены учитываются как заказы по ТЗ.
        if baseline_start <= order_day <= baseline_end:
            baseline_counts[article] = baseline_counts.get(article, 0) + 1
        elif order_day == yesterday:
            yesterday_counts[article] = yesterday_counts.get(article, 0) + 1

    result: list[DropRow] = []

    # Товар должен иметь историю в базовом периоде, иначе сравнивать не с чем.
    for article in known_articles:
        baseline_total = baseline_counts.get(article, 0)
        if baseline_total <= 0:
            continue

        avg = baseline_total / 14.0
        y_count = yesterday_counts.get(article, 0)

        # При avg > 0 формула корректна, включая y_count=0 => 100%.
        drop = ((avg - y_count) / avg) * 100.0

        # "Падение больше чем на 15%" => строго > threshold.
        if drop > threshold:
            result.append(
                DropRow(
                    supplier_article=article,
                    average_per_day=avg,
                    yesterday_orders=y_count,
                    drop_percent=drop,
                    absolute_drop=avg - y_count,
                )
            )

    if sort_mode == "average":
        result.sort(key=lambda x: (x.average_per_day, x.drop_percent), reverse=True)
    elif sort_mode == "absolute":
        result.sort(key=lambda x: (x.absolute_drop, x.drop_percent), reverse=True)
    else:
        result.sort(key=lambda x: (x.drop_percent, x.average_per_day), reverse=True)

    return CabinetReport(
        cabinet_code=cabinet_code,
        cabinet_name=cabinet_name,
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        yesterday=yesterday,
        threshold=threshold,
        rows=result,
    )

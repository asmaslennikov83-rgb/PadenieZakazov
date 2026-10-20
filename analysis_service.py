from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from analytics_client import AnalyticsError, analytics_client
from stock_history_client import StockHistoryError, stock_history_client
from wb_client import wb_orders_client, parse_wb_order_date
from data_quality import nm_id as normalize_nm_id, complete_sum, percent

logger = logging.getLogger(__name__)


@dataclass
class DropRow:
    supplier_article: str
    average_per_day: float
    yesterday_orders: int
    drop_percent: float
    absolute_drop: float
    orders_first_7_avg: float
    orders_last_7_avg: float
    orders_7d_change_percent: float | None
    price_first_7_avg: float | None
    price_last_7_avg: float | None
    price_change_percent: float | None
    yesterday_price_avg: float | None
    fbo_stock_yesterday: int | None
    fbs_stock_yesterday: int | None
    nm_ids: tuple[int, ...]
    views_first_7: int | None = None
    views_last_7: int | None = None
    views_change_percent: float | None = None
    carts_first_7: int | None = None
    carts_last_7: int | None = None
    carts_change_percent: float | None = None
    view_to_cart_first_7: float | None = None
    view_to_cart_last_7: float | None = None
    cart_to_order_first_7: float | None = None
    cart_to_order_last_7: float | None = None
    wishlist_first_7: int | None = None
    wishlist_last_7: int | None = None
    search_position_first_7: float | None = None
    search_position_last_7: float | None = None
    search_position_change: float | None = None
    data_status: str = ""


@dataclass
class CabinetReport:
    cabinet_code: str
    cabinet_name: str
    baseline_start: date
    baseline_end: date
    yesterday: date
    threshold: int
    rows: list[DropRow]
    stock_history_available: bool = False
    funnel_available: bool = False
    search_available: bool = False
    diagnostics: list[tuple[str, str]] = field(default_factory=list)


def _article(row: dict[str, Any]) -> str:
    value = row.get("supplierArticle") or row.get("vendorCode") or row.get("article") or ""
    value = str(value).strip()
    if value:
        return value
    nm_id = row.get("nmId", row.get("nmID"))
    return f"nmID {nm_id}" if nm_id else "Без артикула"


def _price(order: dict[str, Any]) -> float | None:
    for key in ("priceWithDisc",):
        try:
            price = float(order.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            return price
    return None


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pct_change(old: float | None, new: float | None) -> float | None:
    if old is None or new is None or old == 0:
        return None
    return ((new - old) / old) * 100.0


async def build_cabinet_report(cabinet_code: str, cabinet_name: str, threshold: int, sort_mode: str, today_msk: date, force_refresh: bool = False) -> CabinetReport:
    yesterday = today_msk - timedelta(days=1)
    baseline_end = yesterday - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=13)
    split_day = baseline_start + timedelta(days=7)

    orders = await wb_orders_client.get_period_orders(cabinet_code, baseline_start, yesterday, force_refresh=force_refresh)

    baseline_counts: dict[str, int] = {}
    yesterday_counts: dict[str, int] = {}
    first7_counts: dict[str, int] = {}
    last7_counts: dict[str, int] = {}
    first7_prices: dict[str, list[float]] = {}
    last7_prices: dict[str, list[float]] = {}
    yesterday_prices: dict[str, list[float]] = {}
    article_nm_ids: dict[str, set[int]] = {}
    known_articles: set[str] = set()

    for order in orders:
        article = _article(order)
        known_articles.add(article)
        nm_id = normalize_nm_id(order.get("nmId", order.get("nmID")))
        if nm_id:
            article_nm_ids.setdefault(article, set()).add(nm_id)
        parsed_day = parse_wb_order_date(order.get("date"))
        if parsed_day is None:
            continue
        order_day = parsed_day.date()
        price = _price(order)
        if baseline_start <= order_day <= baseline_end:
            baseline_counts[article] = baseline_counts.get(article, 0) + 1
            if order_day < split_day:
                first7_counts[article] = first7_counts.get(article, 0) + 1
                if price is not None:
                    first7_prices.setdefault(article, []).append(price)
            else:
                last7_counts[article] = last7_counts.get(article, 0) + 1
                if price is not None:
                    last7_prices.setdefault(article, []).append(price)
        elif order_day == yesterday:
            yesterday_counts[article] = yesterday_counts.get(article, 0) + 1
            if price is not None:
                yesterday_prices.setdefault(article, []).append(price)

    result: list[DropRow] = []
    for article in known_articles:
        baseline_total = baseline_counts.get(article, 0)
        if baseline_total <= 0:
            continue
        avg = baseline_total / 14.0
        y_count = yesterday_counts.get(article, 0)
        drop = ((avg - y_count) / avg) * 100.0
        if drop <= threshold:
            continue
        first_avg = first7_counts.get(article, 0) / 7.0
        last_avg = last7_counts.get(article, 0) / 7.0
        p_first = _avg(first7_prices.get(article, []))
        p_last = _avg(last7_prices.get(article, []))
        result.append(DropRow(
            supplier_article=article,
            average_per_day=avg,
            yesterday_orders=y_count,
            drop_percent=drop,
            absolute_drop=avg-y_count,
            orders_first_7_avg=first_avg,
            orders_last_7_avg=last_avg,
            orders_7d_change_percent=_pct_change(first_avg, last_avg),
            price_first_7_avg=p_first,
            price_last_7_avg=p_last,
            price_change_percent=_pct_change(p_first, p_last),
            yesterday_price_avg=_avg(yesterday_prices.get(article, [])),
            fbo_stock_yesterday=None,
            fbs_stock_yesterday=None,
            nm_ids=tuple(sorted(article_nm_ids.get(article, set()))),
        ))

    diagnostics = []
    owners = {}
    for article, ids in article_nm_ids.items():
        for n in ids:
            owners.setdefault(n, set()).add(article)
    ambiguous = {n for n, articles in owners.items() if len(articles) > 1}
    if ambiguous:
        diagnostics.append(("Сопоставление", f"Неоднозначные nmId (несколько артикулов продавца): {sorted(ambiguous)}. Дополнительные метрики этих строк не агрегируются."))
    funnel_available = search_available = stock_history_available = False
    first_funnel, last_funnel, search, fbo, fbs = {}, {}, {}, {}, {}
    if result:
        nm_ids = sorted({nm for row in result for nm in row.nm_ids})
        diagnostics.append(("Сопоставление", f"{len(nm_ids)} уникальных nmId; строк без nmId: {sum(not r.nm_ids for r in result)}"))
        if nm_ids:
            try:
                first_funnel, last_funnel = await analytics_client.get_funnel_periods(
                    cabinet_code, baseline_start, split_day - timedelta(days=1), split_day, baseline_end, nm_ids)
                diagnostics.append(("Воронка", f"nmId: первый период {len(first_funnel)}/{len(nm_ids)}, последний {len(last_funnel)}/{len(nm_ids)}"))
            except Exception as exc:
                logger.warning("Воронка %s: %s", cabinet_code, exc)
                diagnostics.append(("Воронка", f"НД: {exc}"))
            try:
                search = await analytics_client.get_search_positions(cabinet_code, split_day, baseline_end,
                    baseline_start, split_day - timedelta(days=1), nm_ids)
                diagnostics.append(("Поиск", f"Получены позиции для {len(search)}/{len(nm_ids)} nmId"))
            except Exception as exc:
                logger.warning("Поиск %s: %s", cabinet_code, exc)
                diagnostics.append(("Поиск", f"НД: {exc}"))
            # Независимые источники: сбой FBS не уничтожает уже полученный FBO.
            for stock_type, target in (("wb", fbo), ("mp", fbs)):
                try:
                    values = await stock_history_client.get_stock_for_day(cabinet_code, yesterday, nm_ids, stock_type)
                    target.update(values)
                    diagnostics.append((f"Остатки {stock_type}", f"Сопоставлено {len(set(values) & set(nm_ids))}/{len(nm_ids)} nmId"))
                except Exception as exc:
                    logger.warning("Остатки %s %s: %s", cabinet_code, stock_type, exc)
                    diagnostics.append((f"Остатки {stock_type}", f"НД: {exc}"))
        for row in result:
            ids = () if any(n in ambiguous for n in row.nm_ids) else row.nm_ids
            for suffix, source in (("first_7", first_funnel), ("last_7", last_funnel)):
                values = {}
                for name in ("views", "carts", "orders", "wishlist"):
                    values[name] = complete_sum(getattr(source[n], name) if n in source else None for n in ids)
                setattr(row, "views_" + suffix, values["views"])
                setattr(row, "carts_" + suffix, values["carts"])
                setattr(row, "wishlist_" + suffix, values["wishlist"])
                setattr(row, "view_to_cart_" + suffix, percent(values["carts"], values["views"]))
                setattr(row, "cart_to_order_" + suffix, percent(values["orders"], values["carts"]))
            row.views_change_percent = _pct_change(row.views_first_7, row.views_last_7)
            row.carts_change_percent = _pct_change(row.carts_first_7, row.carts_last_7)
            row.fbo_stock_yesterday = complete_sum(fbo.get(n) for n in ids)
            row.fbs_stock_yesterday = complete_sum(fbs.get(n) for n in ids)
            # Средние позиции нельзя суммировать или усреднять без весов показов.
            if len(ids) == 1 and ids[0] in search:
                row.search_position_first_7 = search[ids[0]].avg_position_past
                row.search_position_last_7 = search[ids[0]].avg_position
                if row.search_position_first_7 is not None and row.search_position_last_7 is not None:
                    row.search_position_change = row.search_position_last_7 - row.search_position_first_7
            statuses = []
            if not ids:
                statuses.append("Не найден однозначный nmId")
            if len(ids) > 1:
                statuses.append("Позиция НД: несколько nmId, нет весов показов")
            for label, value in (("FBO", row.fbo_stock_yesterday), ("FBS", row.fbs_stock_yesterday),
                                 ("просмотры 1", row.views_first_7), ("просмотры 2", row.views_last_7),
                                 ("корзины 1", row.carts_first_7), ("корзины 2", row.carts_last_7),
                                 ("избранное 1", row.wishlist_first_7), ("избранное 2", row.wishlist_last_7),
                                 ("позиция 1", row.search_position_first_7), ("позиция 2", row.search_position_last_7)):
                if value is None:
                    statuses.append(label + ": НД")
            row.data_status = "; ".join(statuses) or "Данные получены"
        stock_history_available = any(r.fbo_stock_yesterday is not None or r.fbs_stock_yesterday is not None for r in result)
        funnel_available = any(r.views_first_7 is not None or r.views_last_7 is not None for r in result)
        search_available = any(r.search_position_first_7 is not None or r.search_position_last_7 is not None for r in result)

    if sort_mode == "average":
        result.sort(key=lambda x: (x.average_per_day, x.drop_percent), reverse=True)
    elif sort_mode == "absolute":
        result.sort(key=lambda x: (x.absolute_drop, x.drop_percent), reverse=True)
    else:
        result.sort(key=lambda x: (x.drop_percent, x.average_per_day), reverse=True)

    return CabinetReport(cabinet_code, cabinet_name, baseline_start, baseline_end, yesterday, threshold, result, stock_history_available, funnel_available, search_available, diagnostics)

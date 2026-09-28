from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from analytics_client import AnalyticsError, analytics_client
from stock_history_client import StockHistoryError, stock_history_client
from wb_client import wb_orders_client

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


def _article(row: dict[str, Any]) -> str:
    value = row.get("supplierArticle") or row.get("vendorCode") or row.get("article") or ""
    value = str(value).strip()
    if value:
        return value
    nm_id = row.get("nmId")
    return f"nmID {nm_id}" if nm_id else "Без артикула"


def _price(order: dict[str, Any]) -> float | None:
    for key in ("priceWithDisc", "finishedPrice"):
        try:
            price = float(order.get(key))
        except (TypeError, ValueError):
            continue
        if price > 0:
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
        try:
            nm_id = int(order.get("nmId"))
        except (TypeError, ValueError):
            nm_id = 0
        if nm_id:
            article_nm_ids.setdefault(article, set()).add(nm_id)
        try:
            order_day = date.fromisoformat(str(order.get("date", ""))[:10])
        except ValueError:
            continue
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

    funnel_available = search_available = stock_history_available = False
    if result:
        nm_ids = sorted({nm for row in result for nm in row.nm_ids})
        if nm_ids:
            try:
                first_funnel = await analytics_client.get_funnel_history(cabinet_code, baseline_start, split_day - timedelta(days=1), nm_ids)
                await asyncio.sleep(21)
                last_funnel = await analytics_client.get_funnel_history(cabinet_code, split_day, baseline_end, nm_ids)
                funnel_available = bool(first_funnel or last_funnel)
                for row in result:
                    fv = fc = fo = fw = lv = lc = lo = lw = 0
                    for nm in row.nm_ids:
                        fm, lm = first_funnel.get(nm), last_funnel.get(nm)
                        if fm:
                            fv += fm.views; fc += fm.carts; fo += fm.orders; fw += fm.wishlist
                        if lm:
                            lv += lm.views; lc += lm.carts; lo += lm.orders; lw += lm.wishlist
                    if funnel_available:
                        row.views_first_7, row.views_last_7 = fv, lv
                        row.views_change_percent = _pct_change(fv, lv)
                        row.carts_first_7, row.carts_last_7 = fc, lc
                        row.carts_change_percent = _pct_change(fc, lc)
                        row.view_to_cart_first_7 = fc / fv * 100 if fv else None
                        row.view_to_cart_last_7 = lc / lv * 100 if lv else None
                        row.cart_to_order_first_7 = fo / fc * 100 if fc else None
                        row.cart_to_order_last_7 = lo / lc * 100 if lc else None
                        row.wishlist_first_7, row.wishlist_last_7 = fw, lw
            except Exception as exc:
                logger.warning("Воронка недоступна для %s: %s", cabinet_code, exc)

            try:
                await asyncio.sleep(21)
                search = await analytics_client.get_search_positions(cabinet_code, split_day, baseline_end, baseline_start, split_day - timedelta(days=1), nm_ids)
                search_available = bool(search)
                for row in result:
                    cur = [search[n].avg_position for n in row.nm_ids if n in search and search[n].avg_position is not None]
                    past = [search[n].avg_position_past for n in row.nm_ids if n in search and search[n].avg_position_past is not None]
                    if cur:
                        row.search_position_last_7 = sum(cur)/len(cur)
                    if past:
                        row.search_position_first_7 = sum(past)/len(past)
                    if row.search_position_last_7 is not None and row.search_position_first_7 is not None:
                        row.search_position_change = row.search_position_last_7 - row.search_position_first_7
            except Exception as exc:
                logger.warning("Поисковая аналитика недоступна для %s: %s", cabinet_code, exc)

            try:
                await asyncio.sleep(21)
                fbo = await stock_history_client.get_stock_for_day(cabinet_code, yesterday, nm_ids, "wb")
                await asyncio.sleep(21)
                fbs = await stock_history_client.get_stock_for_day(cabinet_code, yesterday, nm_ids, "mp")
                stock_history_available = bool(fbo or fbs)
                for row in result:
                    row.fbo_stock_yesterday = sum(fbo.get(n, 0) for n in row.nm_ids)
                    row.fbs_stock_yesterday = sum(fbs.get(n, 0) for n in row.nm_ids)
            except Exception as exc:
                logger.warning("История остатков недоступна для %s: %s", cabinet_code, exc)

    if sort_mode == "average":
        result.sort(key=lambda x: (x.average_per_day, x.drop_percent), reverse=True)
    elif sort_mode == "absolute":
        result.sort(key=lambda x: (x.absolute_drop, x.drop_percent), reverse=True)
    else:
        result.sort(key=lambda x: (x.drop_percent, x.average_per_day), reverse=True)

    return CabinetReport(cabinet_code, cabinet_name, baseline_start, baseline_end, yesterday, threshold, result, stock_history_available, funnel_available, search_available)

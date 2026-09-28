from __future__ import annotations

from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter

from analysis_service import CabinetReport


def format_number(value: float) -> str:
    if abs(value - round(value)) < 0.005:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_report_message(report: CabinetReport, limit: int = 20) -> str:
    if not report.rows:
        return (
            f"✅ <b>{report.cabinet_name}</b>\n"
            f"За {report.yesterday:%d.%m.%Y} падения заказов более {report.threshold}% не обнаружено."
        )
    lines = [
        "📉 <b>ПАДЕНИЕ ЗАКАЗОВ WILDBERRIES</b>", "",
        f"📅 Вчера: <b>{report.yesterday:%d.%m.%Y}</b>",
        f"🏢 Кабинет: <b>{report.cabinet_name}</b>",
        f"📊 Среднее: {report.baseline_start:%d.%m.%Y}–{report.baseline_end:%d.%m.%Y}",
        f"⚙️ Порог: более {report.threshold}%", "",
    ]
    for i, row in enumerate(report.rows[:limit], 1):
        icon = "🔴" if row.yesterday_orders == 0 else "🔻"
        lines += [
            f"<b>{i}. {html_escape(row.supplier_article)}</b>",
            f"Среднее в день: {format_number(row.average_per_day)} шт.",
            f"Заказов вчера: {row.yesterday_orders} шт.",
            f"{icon} Падение: {format_number(row.drop_percent)}%", "",
        ]
    if len(report.rows) > limit:
        lines.append(f"Показано: {limit} из {len(report.rows)} позиций.")
    lines.append(f"Всего артикулов с падением: <b>{len(report.rows)}</b>")
    extras = ["цена"]
    if report.funnel_available: extras.append("воронка")
    if report.stock_history_available: extras.append("остатки")
    if report.search_available: extras.append("поиск")
    lines.append("В Excel: " + ", ".join(extras) + ".")
    return "\n".join(lines)


def html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rb(value: float | None, digits: int = 2):
    return "" if value is None else round(value, digits)


def _ib(value: int | None):
    return "" if value is None else value


def build_excel(reports: list[CabinetReport]) -> bytes:
    wb = Workbook(); wb.remove(wb.active)
    headers = [
        "Кабинет", "Артикул продавца", "Среднее заказов в день (14 дн.)", "Заказов вчера", "Падение, %", "Абсолютное снижение",
        "Заказы/день — первые 7 дней", "Заказы/день — последние 7 дней", "Изменение заказов 7д→7д, %",
        "Средняя цена — первые 7 дней, ₽", "Средняя цена — последние 7 дней, ₽", "Изменение цены 7д→7д, %", "Средняя цена вчера, ₽",
        "Остаток FBO вчера, шт.", "Остаток FBS вчера, шт.", "Остаток всего вчера, шт.",
        "Просмотры — первые 7 дней", "Просмотры — последние 7 дней", "Изменение просмотров, %",
        "Добавления в корзину — первые 7 дней", "Добавления в корзину — последние 7 дней", "Изменение корзин, %",
        "Конверсия просмотр→корзина — первые 7 дней, %", "Конверсия просмотр→корзина — последние 7 дней, %",
        "Конверсия корзина→заказ — первые 7 дней, %", "Конверсия корзина→заказ — последние 7 дней, %",
        "Добавления в избранное — первые 7 дней", "Добавления в избранное — последние 7 дней",
        "Средняя позиция в поиске — первые 7 дней", "Средняя позиция в поиске — последние 7 дней", "Изменение позиции (+: хуже)",
        "Базовый период с", "Базовый период по", "Дата сравнения",
    ]
    for report in reports:
        ws = wb.create_sheet(sanitize_sheet_name(report.cabinet_name)); ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True); c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for row in report.rows:
            total_stock = "" if row.fbo_stock_yesterday is None and row.fbs_stock_yesterday is None else (row.fbo_stock_yesterday or 0) + (row.fbs_stock_yesterday or 0)
            ws.append([
                report.cabinet_name, row.supplier_article, round(row.average_per_day,2), row.yesterday_orders, round(row.drop_percent,2), round(row.absolute_drop,2),
                round(row.orders_first_7_avg,2), round(row.orders_last_7_avg,2), _rb(row.orders_7d_change_percent),
                _rb(row.price_first_7_avg), _rb(row.price_last_7_avg), _rb(row.price_change_percent), _rb(row.yesterday_price_avg),
                _ib(row.fbo_stock_yesterday), _ib(row.fbs_stock_yesterday), total_stock,
                _ib(row.views_first_7), _ib(row.views_last_7), _rb(row.views_change_percent),
                _ib(row.carts_first_7), _ib(row.carts_last_7), _rb(row.carts_change_percent),
                _rb(row.view_to_cart_first_7), _rb(row.view_to_cart_last_7), _rb(row.cart_to_order_first_7), _rb(row.cart_to_order_last_7),
                _ib(row.wishlist_first_7), _ib(row.wishlist_last_7),
                _rb(row.search_position_first_7), _rb(row.search_position_last_7), _rb(row.search_position_change),
                report.baseline_start.strftime("%d.%m.%Y"), report.baseline_end.strftime("%d.%m.%Y"), report.yesterday.strftime("%d.%m.%Y"),
            ])
        widths = [24,28,24,18,16,22,24,24,24,25,25,24,22,20,20,22,22,22,22,25,25,22,27,27,27,27,25,25,28,28,24,18,18,18]
        for i,w in enumerate(widths,1): ws.column_dimensions[get_column_letter(i)].width=w
        ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions; ws.row_dimensions[1].height=60
    if not wb.sheetnames:
        ws=wb.create_sheet("Отчёт"); ws["A1"]="Нет данных"
    stream=BytesIO(); wb.save(stream); return stream.getvalue()


def sanitize_sheet_name(value: str) -> str:
    for ch in '[]:*?/\\': value=value.replace(ch,' ')
    return (value.strip() or "Отчёт")[:31]

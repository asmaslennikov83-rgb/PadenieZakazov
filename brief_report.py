"""Small SQL overview: no full event/daily export loaded into Python memory."""
from datetime import datetime,timedelta
from html import escape
from zoneinfo import ZoneInfo
from business_report import usable_orders
from config import CABINETS


def brief(store,day,cabinets):
    start=day-timedelta(days=14)
    latest={}
    for row in store.sources():
        if row['cabinet_id'] in cabinets:latest[row['cabinet_id'],row['source']]=row
    def num(value):return 'Н/Д' if value is None else f'{value:,.2f}'.rstrip('0').rstrip('.')
    lines=[f'<b>WB: {day} · v3.0.2 · краткий обзор</b>',
           'Сформировано: '+datetime.now(ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M МСК'),
           'Расчёт из сохранённой базы. Кнопка не обращается к WB. Заказы — единицы, отмены включены.']
    for cabinet in cabinets:
        rows=store.rows("""SELECT day,count(*) AS units,SUM(json_extract(payload,'$.order_price')) AS amount,
            SUM(CASE WHEN json_extract(payload,'$.order_price') IS NULL THEN 1 ELSE 0 END) AS missing_prices
            FROM v3_events WHERE cabinet_id=? AND source='orders' AND day BETWEEN ? AND ? GROUP BY day""",(cabinet,str(start),str(day)))
        data={r['day']:r for r in rows}
        covered={r['day'] for r in store.rows("SELECT day FROM v3_coverage WHERE cabinet_id=? AND source='orders' AND status='OK' AND day BETWEEN ? AND ?",(cabinet,str(start),str(day)))}
        source=latest.get((cabinet,'orders'),{})
        complete=len(covered)==15 and usable_orders(source)
        known=data.get(str(day),{}).get('units',0)
        base=sum(r['units'] for r in rows if r['day']!=str(day))/14
        amount=data.get(str(day),{}).get('amount',0)
        missing=data.get(str(day),{}).get('missing_prices',0)
        lines.append('<b>'+escape(CABINETS[cabinet].name)+'</b>')
        lines.append(f'Единицы: {known if complete else "Н/Д"}; известный подытог {known}. Покрытие {len(covered)}/15 дней.')
        lines.append(f'База/день: {num(base if complete else None)}. Изменение: {num(known-base if complete else None)} шт.; '+(f'{known/base-1:+.1%}' if complete and base>0 else 'Н/Д'))
        lines.append('Сумма заказов: '+num(amount if complete and not missing else None)+' ₽.')
        last=source.get('last_success_at') or (source.get('completed_at') if source.get('status')=='OK' else None)
        if last:lines.append('Последнее подтверждение источника: '+escape(str(last)))
        if not complete or source.get('phase') in {'RUNNING','QUEUED','WAITING_RETRY'}:
            lines.append(escape(source.get('reason','Загрузка ещё не завершена; полного итога нет.')))
    lines.append('Это краткий обзор за вчера и базу 14 дней. Полная история, день недели, товары и гипотезы — отдельный пакет Excel. /quality — источники; /refresh — загрузка; /diagnostics — журнал.')
    return '\n'.join(lines)

"""New read-only screens; legacy handlers/settings remain in bot.py."""
from __future__ import annotations
import asyncio
from datetime import datetime,timedelta
from html import escape
import json
import logging
from pathlib import Path
import time
from zoneinfo import ZoneInfo
from aiogram import Router,F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile,InlineKeyboardButton,InlineKeyboardMarkup
from config import ALLOWED_TELEGRAM_IDS,CABINETS,DB_PATH
from db import get_user_settings
from history_store import Store,key,now
from collector import Collector
from business_report import build,telegram
from analytical_export import build_package
from runtime_diagnostics import stage

router=Router()
store=Store(DB_PATH)
collector=Collector(store)
TZ=ZoneInfo('Europe/Moscow')
cache={}
jobs=set()
check_jobs={}
build_task=None
build_signature=None


def selected(mode):return ['spb_tt','spb_tt_group'] if mode=='both' else [mode]


def menu():
    rows=[ [('Обзор','overview'),('Ситуация сейчас','now')], [('Падения','drops'),('Рост и возможности','growth')],
           [('Риски остатков','stocks'),('Проверить товар','product')], [('Сравнить кабинеты','compare'),('Реклама','ads')],
           [('Отчёт 7 дней','period7'),('14 дней','period14'),('30 дней','period30')],
           [('Все товары','all'),('Качество данных','quality'),('Excel','excel')], [('Обновить данные','refresh')],
           [('Прежний отчёт','legacy'),('Настройки','settings')] ]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data='v3:'+v) for t,v in row] for row in rows])


async def allowed(event):
    user=event.from_user
    if not user or user.id not in ALLOWED_TELEGRAM_IDS:
        await event.answer('Нет доступа',**({'show_alert':True} if hasattr(event,'data') else {}))
        return False
    return True


async def report_for(user_id,refresh=False):
    global build_task,build_signature
    settings=await get_user_settings(user_id)
    day=datetime.now(TZ).date()-timedelta(days=1)
    codes=selected(settings.cabinet_mode)
    if refresh:await collector.sync(codes,extras=False)
    sources=[s for s in store.sources() if s['cabinet_id'] in codes]
    latest={}
    for s in sources:latest[s['cabinet_id'],s['source']]=s
    signature=(user_id,str(day),settings.threshold,settings.cabinet_mode,json.dumps(store.preference(user_id),sort_keys=True),key(sorted(latest.items())))
    saved=cache.get(signature)
    if not refresh and saved and time.monotonic()-saved[0]<120:return saved[1]
    if build_task is not None and not build_task.done() and build_signature!=signature:
        raise RuntimeError('REPORT_BUILD_BUSY')
    if build_task is None or build_task.done():
        build_signature=signature
        build_task=asyncio.create_task(asyncio.to_thread(build,store,day,codes,settings.threshold,store.preference(user_id)))
    report=await asyncio.shield(build_task)
    for old in list(cache):
        if old[0]==user_id or time.monotonic()-cache[old][0]>=120:cache.pop(old,None)
    cache[signature]=(time.monotonic(),report)
    return report


async def send_package(bot,chat_id,report):
    name,data=await asyncio.to_thread(build_package,report)
    await bot.send_document(chat_id,BufferedInputFile(data,filename=name),caption='Самостоятельный аналитический пакет: факты, история, словарь, ограничения и действия.')


async def quick_check(bot,chat_id,user_id):
    """Old check button now reads stored facts; network refresh has its own queue."""
    if user_id in check_jobs and not check_jobs[user_id].done():
        await bot.send_message(chat_id,'Предыдущий расчёт ещё выполняется. Повторный расчёт и новые запросы WB не запущены. /quality — загрузка.');return
    async def run():
        operation=key('check',user_id,time.monotonic());started=time.monotonic()
        await bot.send_message(chat_id,'Формирую обзор из сохранённых данных. Загрузка WB идёт отдельно. Если данных ещё нет, это будет указано в обзоре. Версия 3.0.2.')
        stage(store.path,operation,'report_start')
        task=asyncio.create_task(quick_report_for(user_id))
        try:
            for attempt in range(3):
                done,_=await asyncio.wait({task},timeout=30)
                if done:break
                stage(store.path,operation,'report_wait',elapsed_seconds=round(time.monotonic()-started))
                await bot.send_message(chat_id,'Расчёт по локальной базе ещё выполняется. Ожидание на этом этапе не означает запрос или блокировку WB.')
            if not task.done():
                task.cancel()
                stage(store.path,operation,'report_timeout',elapsed_seconds=round(time.monotonic()-started))
                await bot.send_message(chat_id,'Расчёт локального отчёта превысил 90 секунд. Новые запросы WB этой кнопкой не запускались. Нужны логи Bothost и data/logs/runtime.log: возможны нехватка памяти, медленная база или остановка процесса. Не перезапускайте бота многократно.');return
            text=task.result()
            stage(store.path,operation,'report_ready',elapsed_seconds=round(time.monotonic()-started))
            await bot.send_message(chat_id,text,reply_markup=menu())
            await bot.send_message(chat_id,'Обзор готов. Полный пакет — кнопка Excel; обновление источников — «Обновить данные».')
            stage(store.path,operation,'message_sent',elapsed_seconds=round(time.monotonic()-started))
        except asyncio.CancelledError:
            task.cancel();raise
        except Exception as exc:
            stage(store.path,operation,'failed',error_type=type(exc).__name__,elapsed_seconds=round(time.monotonic()-started))
            await bot.send_message(chat_id,'Не удалось построить или отправить обзор. Код диагностики: '+operation+'. Проверьте журнал Bothost и data/logs/runtime.log. Токены и базу в чат присылать не нужно.')
    check_jobs[user_id]=asyncio.create_task(run())
    def finish(task):
        if check_jobs.get(user_id) is task:check_jobs.pop(user_id,None)
        if not task.cancelled():task.exception()
    check_jobs[user_id].add_done_callback(finish)


async def quick_report_for(user_id):
    from brief_report import brief
    settings=await get_user_settings(user_id)
    day=datetime.now(TZ).date()-timedelta(days=1)
    return await asyncio.to_thread(brief,store,day,selected(settings.cabinet_mode))


async def send_report(bot,chat_id,settings,force_refresh=False,scheduled=False):
    report=await report_for(settings.user_id,refresh=force_refresh)
    delivery=key('daily',settings.user_id,report['report_date']) if scheduled else None
    if delivery and not store.delivery(delivery):return
    try:
        message=await bot.send_message(chat_id,telegram(report,{k:v.name for k,v in CABINETS.items()}),reply_markup=menu())
        if delivery:store.delivery(delivery,'SENT',message.message_id)
        if scheduled:store.sync_state('user:'+str(settings.user_id),'daily_report',{'report_date':report['report_date'],'run_id':report['run_id'],'status':report['status'],'cabinets':report['cabinets'],'known_units':sum(s['known_ordered_units'] for s in report['summary'])})
        attachment=key('daily-file',settings.user_id,report['report_date']) if scheduled else None
        if not attachment or store.delivery(attachment):
            try:
                await send_package(bot,chat_id,report)
                if attachment:store.delivery(attachment,'SENT')
            except Exception:
                if attachment:store.delivery(attachment,'UNKNOWN')
                await bot.send_message(chat_id,'Обзор отправлен. Пакет не удалось доставить. Он доступен кнопкой Excel; автоматический повтор не выполняется, чтобы избежать дубля.')
    except Exception:
        if delivery:store.delivery(delivery,'UNKNOWN')
        raise


def current_stocks(report):
    data={}
    current=datetime.now(TZ)
    for row in report['tables']['StockSnapshots']:
        if row.get('source') not in {'stocks_wb','stocks_mp'}:continue
        scope=(row['cabinet_id'],row['nm_id'],row['source'])
        stamp=row['snapshot_at']
        if scope not in data or stamp>data[scope][0]:data[scope]=(stamp,[])
        if stamp==data[scope][0]:data[scope][1].append(row)
    result=[]
    lookup={(p['cabinet_id'],p['nm_id']):p for p in report['all_products']}
    sources={(s['cabinet_id'],s['source']):s for s in report['tables']['SourceStatus'] if s.get('is_latest',True)}
    for (c,n,source),(stamp,rows) in data.items():
        age=(current-datetime.fromisoformat(stamp).astimezone(TZ)).total_seconds()/3600
        status='OK' if 0<=age<=2 and sources.get((c,source),{}).get('status')=='OK' else 'STALE'
        product=lookup.get((c,n),{});speed=product.get('stock_demand_units_per_day')
        qty=sum(r['quantity'] for r in rows)
        result.append({'cabinet_id':c,'nm_id':n,'source':source,'quantity':qty,'snapshot_at':stamp,'status':status,'coverage_days':qty/speed if speed and speed>0 and status=='OK' else None})
    return result


def screen(report,kind):
    def num(v):return 'Н/Д' if v is None else f'{v:,.2f}'.rstrip('0').rstrip('.')
    rows=report['all_products'];lines=[]
    if kind=='overview':return telegram(report,{k:v.name for k,v in CABINETS.items()})
    if kind in {'drops','all'}:
        selected_rows=rows if kind=='all' else sorted([r for r in rows if r['legacy_drop_selected']],key=lambda r:r['priority_absolute'] or 0,reverse=True)
        lines=[f'<b>{"Все товары" if kind=="all" else "Падения > установленного порога"}: {len(selected_rows)}</b>', 'Здесь до 20 строк; полный ассортимент — кнопка Excel.']
        for r in selected_rows[:20]:lines.append(f'{escape(r["cabinet_id"])} / {escape(r["supplier_article"] or r["nm_id"])} [{r["nm_id"]}]: {num(r["ordered_units"])} шт.; Δ {num(r["delta_units"])}; {escape(r["signal"])}')
    elif kind=='growth':
        evidence=[e for e in report['tables']['Evidence'] if e['category']=='Проверить возможность роста']
        lines=['<b>Рост и возможности</b>','Основания для проверки; прибыльность и причинный эффект не утверждаются.']
        for e in evidence[:12]:lines.append(f'{escape(e["cabinet_id"])} / {e["nm_id"]}: {escape(e["fact"])}. {escape(e["next_action"])}')
        if not evidence:lines.append('Оснований для отдельной возможности пока недостаточно. Проверьте полноту истории.')
    elif kind in {'stocks','now'}:
        snapshots=current_stocks(report)
        lines=['<b>Ситуация сейчас: последние снимки</b>','Исторический отчёт относится к завершённому дню. Сегодняшние заказы не сравниваются с полным днём.']
        if kind=='now':
            today=datetime.now(TZ).date().isoformat()
            for c in report['cabinets']:
                totals=store.rows("SELECT count(*) AS units FROM v3_events WHERE cabinet_id=? AND source='orders' AND day=?",(c,today))[0]
                latest=next((s for s in report['tables']['SourceStatus'] if s['cabinet_id']==c and s['source']=='orders' and s.get('is_latest',True)),{})
                lines.append(f'Сегодня {today}, незавершённый день / {c}: известно {totals["units"]} заказанных единиц. Качество {escape(latest.get("status","MISSING"))}, последнее обновление {escape(str(latest.get("last_success_at") or latest.get("completed_at") or "Н/Д"))}. Это накопительный подытог источника с задержкой.')
        if not snapshots:lines.append('Н/Д: снимки остатков ещё не получены. Проверьте доступ к Аналитике.')
        for r in sorted(snapshots,key=lambda x:x['coverage_days'] if x['coverage_days'] is not None else float('inf'))[:18]:
            lines.append(f'{r["cabinet_id"]} / {r["nm_id"]} {r["source"]}: {r["quantity"]} шт.; {r["status"]}; {escape(r["snapshot_at"])}. Оценка покрытия {num(r["coverage_days"])} дней.')
        lines.append('Покрытие каждой модели использует общий спрос карточки и требует проверки распределения спроса; это риск, не доказанный дефицит.')
    elif kind=='quality':
        lines=['<b>Качество и доступность</b>']
        for s in report['tables']['SourceStatus']:
            if s.get('is_latest',True):lines.append(f'{escape(s["cabinet_id"])} / {escape(s["source"])}: {escape(s["status"])} — {escape(s.get("reason", ""))}; записей {s.get("records",0)}.')
        for s in report['summary']:lines.append(f'{s["cabinet_id"]}: история {s["available_days"]}/{s["expected_days"]} дней для основного сравнения.')
        lines.append('Реестр всех метрик и отсутствующих модулей — в пакете. Без доступа в кабинет наличие прав не подтверждено.')
    elif kind=='compare':
        lines=['<b>Подтверждённые пары кабинетов</b>']
        for r in report['comparisons'][:16]:lines.append(f'{escape(r["canonical_product_id"])}: факт {num(r.get("fact_units"))} / база {num(r.get("base_units"))}; Δ {num(r.get("delta_units"))}; {r["status"]}.')
        if not report['comparisons']:lines.append('Н/Д: импортируйте подтверждённые пары ProductMapping с комплектностью и единицей сравнения. По названию товары не сопоставляются.')
    elif kind=='ads':
        ads=report['tables']['AdsDaily'];lines=['<b>Реклама</b>']
        if not ads:lines.append('Н/Д: нет полученной статистики рекламы. Проверьте токен категории Продвижение и SourceStatus/ads. Также доступен импорт AdsDaily и CampaignProducts с окном атрибуции. Органика вычитанием не рассчитывается.')
        else:
            lines.append(f'Наблюдений: {len(ads)}. Исходная детализация, CTR/CPC/ДРР и проверки атрибуции — в Excel.')
            for r in ads[:10]:lines.append(f'{escape(r["cabinet_id"])} / кампания {escape(r["campaign_id"])} / {r["nm_id"]}: расход {num(r.get("spend"))}; {escape(r["attribution_window"])}.')
        for event in report['tables'].get('CampaignEvents',[])[-3:]:lines.append(f'Факт: статус кампании {event["campaign_id"]} изменился {event["before_status"]} → {event["after_status"]}. Влияние на заказы не установлено.')
    elif kind.startswith('period'):
        days=int(kind.removeprefix('period'));records=[r for r in report['tables']['PeriodMetrics'] if r['window_days']==days]
        lines=[f'<b>Равные завершённые периоды по {days} дней</b>']
        if records:
            r=records[0];lines.append(f'{r["period_start"]}–{r["period_end"]} против {r["previous_start"]}–{r["previous_end"]}')
        for c in report['cabinets']:
            rs=[r for r in records if r['cabinet_id']==c];full=all(r['status']=='OK' for r in rs) and bool(rs)
            lines.append(f'{c}: '+(f'{sum(r["units"] for r in rs)} против {sum(r["previous_units"] for r in rs)} шт.' if full else 'Н/Д полного итога: неполная история части товаров; покрытие по строкам в пакете.'))
    return '\n'.join(lines)[:3900]


async def refresh_background():
    await collector.sync(list(CABINETS),extras=True)
    cache.clear()


async def request_refresh(event,bot,user_id,chat_id):
    if jobs:
        await event.answer('Загрузка уже выполняется. /quality — прогресс, /overview — текущий результат.');return
    settings=await get_user_settings(user_id)
    codes=selected(settings.cabinet_mode)
    if any(store.rows('SELECT owner FROM v3_leases WHERE name=? AND expires_at>?',('sync:'+c,now())) for c in codes):
        await event.answer('Уже идёт фоновая загрузка. Повторные запросы не запущены. /quality — прогресс, /overview — текущий результат.');return
    async def run():
        try:
            await collector.sync(codes,extras=True)
            cache.clear()
            await bot.send_message(chat_id,'Проход загрузки завершён. /quality — ошибки и доступность источников; /overview — новый обзор; /excel — новый пакет. Завершение прохода не означает успешность каждого источника.')
        except Exception:
            logging.getLogger(__name__).exception('Manual refresh failed')
            await bot.send_message(chat_id,'Проход загрузки прерван. Сохранённые данные и курсоры остаются в базе. /quality — состояние источников.')
    task=asyncio.create_task(run());jobs.add(task);task.add_done_callback(jobs.discard)
    await event.answer('Запущено обновление заказов и дополнительных источников. Оно может занять несколько минут из-за ограничений WB. Если фоновая загрузка уже идёт, повторные запросы не запускаются. /quality — прогресс.')


@router.message(Command('diagnostics'))
async def diagnostics(message):
    if not await allowed(message):return
    import os
    lines=['Версия 3.0.2; процесс '+str(os.getpid())+'. Эта команда не обращается к WB и не строит отчёт.']
    memory=Path('/proc/self/status')
    if memory.exists():
        lines.extend(line for line in memory.read_text().splitlines() if line.startswith(('VmRSS:','VmHWM:')))
    path=store.path.parent/'logs'/'runtime.log'
    if path.is_file():lines.extend(path.read_text(encoding='utf-8',errors='replace').splitlines()[-12:])
    else:lines.append('Журнал этапов пока не создан.')
    await message.answer(escape('\n'.join(lines)[-3500:]))


@router.callback_query(F.data.startswith('v3:'))
async def view(call,bot):
    if not await allowed(call):return
    await call.answer()
    kind=call.data.split(':',1)[1]
    if kind=='refresh':await request_refresh(call.message,bot,call.from_user.id,call.message.chat.id);return
    if kind=='settings':
        await call.message.answer('Существующие настройки сохранены. /menu — порог, сортировка, время, кабинет.\nДополнительные фильтры: /filters\nПроверить товар: /product nmID или артикул.\nИстория действий: /actions');return
    if kind=='legacy':
        await quick_check(bot,call.message.chat.id,call.from_user.id);return
    if kind=='product':await call.message.answer('Введите /product и nmID или артикул продавца. При совпадениях будут предложены варианты.');return
    report=await report_for(call.from_user.id)
    if kind=='excel':await send_package(bot,call.message.chat.id,report)
    else:await call.message.answer(screen(report,kind),reply_markup=menu())


@router.message(Command('overview','now','drops','growth','stocks','quality','excel','ads','compare','refresh'))
async def command(message,bot):
    if not await allowed(message):return
    kind=message.text.split()[0].split('@')[0][1:]
    if kind=='refresh':await request_refresh(message,bot,message.from_user.id,message.chat.id);return
    report=await report_for(message.from_user.id)
    if kind=='excel':await send_package(bot,message.chat.id,report)
    else:await message.answer(screen(report,kind),reply_markup=menu())


@router.message(Command('product'))
async def product(message):
    if not await allowed(message):return
    query=message.text.partition(' ')[2].strip()
    if not query:await message.answer('Пример: /product 12345678 или /product Артикул продавца');return
    report=await report_for(message.from_user.id)
    matches=[r for r in report['all_products'] if r['nm_id']==query or query.casefold() in r['supplier_article'].casefold()]
    if not matches:await message.answer('Товар не найден в доступном ассортименте. Проверьте каталог и загрузку заказов.');return
    if len(matches)>1:
        buttons=[[InlineKeyboardButton(text=f'{r["cabinet_id"]} / {r["nm_id"]} / {r["supplier_article"][:25]}',callback_data=f'v3p:{r["cabinet_id"]}:{r["nm_id"]}')] for r in matches[:20]]
        await message.answer('Найдено несколько товаров. Выберите кабинет и nmID; полный перечень в Excel.',reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons));return
    await message.answer(product_text(report,matches[0]),reply_markup=menu())


def product_text(report,row):
    lines=[f'<b>{escape(row["supplier_article"] or row["nm_id"])}</b> · {row["cabinet_id"]} / {row["nm_id"]}',
           f'Факт: {row["ordered_units"] if row["ordered_units"] is not None else "Н/Д"}; база: {row["base_14_avg"] if row["base_14_avg"] is not None else "Н/Д"}; {escape(row["signal"])}.',
           f'Покрытие базы: {row["base_14_known_days"]}/{row["base_14_expected_days"]} дней.']
    for e in report['tables']['Evidence']:
        if e['cabinet_id']==row['cabinet_id'] and e['nm_id']==row['nm_id']:lines.append(f'{escape(e["explanation_status"])}: {escape(e["fact"])}. {escape(e["next_action"])}')
    pairs=[r for r in report['comparisons'] if row['nm_id'] in {r.get('nm_a'),r.get('nm_b')} and row['cabinet_id'] in {r.get('cabinet_a'),r.get('cabinet_b')}]
    for p in pairs:lines.append(f'Подтверждённая пара {escape(p["canonical_product_id"])}: общий Δ {p["delta_units"] if p["delta_units"] is not None else "Н/Д"}.')
    lines.append(row['wb_url']);return '\n'.join(lines)[:3900]


@router.callback_query(F.data.startswith('v3p:'))
async def product_selection(call):
    if not await allowed(call):return
    await call.answer();_,c,n=call.data.split(':')
    report=await report_for(call.from_user.id)
    row=next((r for r in report['all_products'] if r['cabinet_id']==c and r['nm_id']==n),None)
    await call.message.answer(product_text(report,row) if row else 'Товар недоступен для выбранных кабинетов.',reply_markup=menu())


@router.message(Command('filters'))
async def filters(message):
    if not await allowed(message):return
    args=message.text.split()[1:]
    if not args:
        await message.answer(escape(json.dumps(store.preference(message.from_user.id),ensure_ascii=False))+'\n/filters min_base 1\n/filters min_absolute 2\n/filters repeat_days 2\n/filters rare_mode separate|hide\n/filters growth_threshold 0.15\n/filters stock_days 7\n/filters urgent true|false');return
    try:
        name,raw=args
        if name not in store.preference(message.from_user.id):raise ValueError()
        value=raw if name=='rare_mode' else raw.lower()=='true' if name=='urgent' else float(raw)
        if name=='rare_mode' and value not in {'separate','hide'}:raise ValueError()
        if name not in {'rare_mode','urgent'} and (not __import__('math').isfinite(value) or value<0 or value>10000):raise ValueError()
        if name=='repeat_days' and (int(value)!=value or not 1<=value<=14):raise ValueError()
        if name=='urgent' and raw.lower() not in {'true','false'}:raise ValueError()
        store.preference(message.from_user.id,{name:value});cache.clear();await message.answer('Настройка сохранена. Все товары остаются в Excel.')
    except (ValueError,TypeError):await message.answer('Не удалось разобрать настройку. /filters — формат и текущие значения.')


@router.message(Command('actions','action'))
async def actions(message):
    if not await allowed(message):return
    if message.text.split()[0].split('@')[0]=='/action':
        args=message.text.split()[1:]
        try:
            aid,status,*rest=args;store.action(aid,status,owner=' '.join(rest) or None);await message.answer('Статус проверки обновлён вручную. Бизнес-действия бот не выполнял.')
        except (ValueError,TypeError):await message.answer('/action ID OPEN|IN_PROGRESS|DONE|CANCELLED [ответственный]')
        return
    report=await report_for(message.from_user.id)
    rows=report['tables']['Actions'][:15]
    text='\n'.join(f'{r["action_id"]} {r["status"]} / {r["nm_id"]}: {escape(r["next_action"])}' for r in rows)
    await message.answer(('<b>Проверки и действия</b>\n'+text+'\n/action ID DONE — ручная отметка. Полная история в БД и пакете.')[:3900])


async def urgent_tick(bot):
    from db import list_user_settings
    for settings in await list_user_settings():
        if settings.user_id not in ALLOWED_TELEGRAM_IDS or not store.preference(settings.user_id).get('urgent'):continue
        report=await report_for(settings.user_id)
        threshold=store.preference(settings.user_id)['stock_days']
        risks=[r for r in current_stocks(report) if r['status']=='OK' and r['coverage_days'] is not None and r['coverage_days']<threshold]
        for r in risks[:3]:
            identity=key('urgent-stock',settings.user_id,r['cabinet_id'],r['nm_id'],r['source'],int(r['coverage_days']))
            if not store.delivery(identity):continue
            try:
                msg=await bot.send_message(settings.user_id,f'Проверить риск запаса: {r["cabinet_id"]} / {r["nm_id"]}, {r["quantity"]} шт. на {escape(r["snapshot_at"])}. Оценка покрытия модели {r["coverage_days"]:.1f} дня по общему спросу карточки; распределение спроса требует проверки. Это не доказанный дефицит.',reply_markup=menu())
                store.delivery(identity,'SENT',msg.message_id)
            except Exception:store.delivery(identity,'UNKNOWN')


async def correction_tick(bot):
    from db import list_user_settings
    for settings in await list_user_settings():
        if settings.user_id not in ALLOWED_TELEGRAM_IDS:continue
        state=store.sync_state('user:'+str(settings.user_id),'daily_report')
        if not state:continue
        report=await report_for(settings.user_id)
        if state.get('report_date')!=report['report_date'] or state.get('cabinets')!=report['cabinets'] or state.get('run_id')==report['run_id']:continue
        units=sum(s['known_ordered_units'] for s in report['summary'])
        significant=report['status']=='OK' and (state.get('status')!='OK' or abs(units-state.get('known_units',units))>=max(3,state.get('known_units',0)*.05))
        if not significant:continue
        identity=key('correction',settings.user_id,report['run_id'])
        if not store.delivery(identity):continue
        try:
            msg=await bot.send_message(settings.user_id,'<b>Уточнение отчёта: новая ревизия</b>\n'+telegram(report,{k:v.name for k,v in CABINETS.items()}),reply_markup=menu())
            store.delivery(identity,'SENT',msg.message_id)
            store.sync_state('user:'+str(settings.user_id),'daily_report',{'report_date':report['report_date'],'run_id':report['run_id'],'status':report['status'],'cabinets':report['cabinets'],'known_units':units})
            await send_package(bot,settings.user_id,report)
        except Exception:store.delivery(identity,'UNKNOWN')

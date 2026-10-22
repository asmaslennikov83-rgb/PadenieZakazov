"""Card-level report from persisted facts, with explicit coverage and rule evidence."""
from __future__ import annotations
from collections import defaultdict
from datetime import date, timedelta, datetime
import json
import uuid
from history_store import Store, now, key
from data_quality import complete_sum, percent
from metric_registry import registry, TABLES

VERSION='3.0.2'


def dates(start,end):
    return [start+timedelta(days=i) for i in range((end-start).days+1)]


def change(old,new):
    return new/old-1 if old is not None and old>0 and new is not None else None


def usable_orders(source):
    return source.get('status')=='OK' or (source.get('phase') in {'RUNNING','QUEUED','WAITING_RETRY'} and bool(source.get('last_success_at')))


def classify(base,fact,new=False,rare_cutoff=1):
    if fact is None or base is None:return 'НЕПОЛНАЯ ИСТОРИЯ' if new else 'Н/Д'
    if base==0:return 'ПОЯВИЛИСЬ ЗАКАЗЫ' if fact>0 else 'НЕТ ЗАКАЗОВ'
    if base<rare_cutoff and fact==0:return 'РЕДКИЙ СПРОС'
    return 'СНИЖЕНИЕ' if fact<base else 'РОСТ' if fact>base else 'БЕЗ ИЗМЕНЕНИЙ'


def latest_sources(store,cabinets):
    latest={}
    for row in store.sources():
        if row['cabinet_id'] in cabinets: latest[row['cabinet_id'],row['source']]=row
    return latest


def build(store:Store,day:date,cabinets:list[str],threshold=15,preferences=None,save=True):
    run=uuid.uuid4().hex
    from zoneinfo import ZoneInfo
    if day>=datetime.now(ZoneInfo('Europe/Moscow')).date():raise ValueError('Аналитический отчёт строится только за завершённый день Europe/Moscow; для текущих снимков используйте Ситуация сейчас')
    pref=preferences or {'min_base':0,'min_absolute':0,'repeat_days':1,'growth_threshold':.15,'stock_days':7,'rare_mode':'separate'}
    start=day-timedelta(days=89)
    target_start=start
    placeholders=','.join('?' for _ in cabinets)
    if not placeholders:raise ValueError('Не выбран кабинет')
    oldest=store.rows(f"SELECT MIN(day) AS first FROM (SELECT day FROM v3_coverage WHERE cabinet_id IN ({placeholders}) AND day<=? UNION ALL SELECT day FROM v3_events WHERE cabinet_id IN ({placeholders}) AND day<=?)",(*cabinets,str(day),*cabinets,str(day)))[0]['first']
    if oldest:start=min(start,date.fromisoformat(oldest))
    source=latest_sources(store,cabinets)
    availability=[json.loads(r['payload']) for r in store.rows("SELECT payload FROM v3_manual WHERE kind='AvailabilityPeriods'")]
    economics=[json.loads(r['payload']) for r in store.rows("SELECT payload FROM v3_manual WHERE kind='Economics'")]
    coverage={(r['cabinet_id'],r['source'],r['day']):r for r in store.rows('SELECT * FROM v3_coverage WHERE day BETWEEN ? AND ?',(str(start),str(day)))}
    products=[]
    for r in store.rows('SELECT * FROM v3_products ORDER BY cabinet_id,nm_id'):
        if r['cabinet_id'] in cabinets:
            products.append({'cabinet_id':r['cabinet_id'],'nm_id':r['nm_id'],**json.loads(r['payload']),'first_observed_at':r['first_seen'],'last_observed_at':r['last_seen']})
    events=[]
    for r in store.rows('SELECT * FROM v3_events WHERE day BETWEEN ? AND ?',(str(start),str(day))):
        if r['cabinet_id'] in cabinets:events.append({**json.loads(r['payload']),'cabinet_id':r['cabinet_id'],'source':r['source'],'source_run_id':r['run_id'],'version_at':r['version_at']})
    by=defaultdict(list)
    for e in events:by[e['cabinet_id'],e['nm_id'],e['day'],e['source']].append(e)
    funnel={(r['cabinet_id'],r['nm_id'],r['day']):{**json.loads(r['payload']),'source_run_id':r['run_id'],'observed_at':r['observed_at']} for r in store.rows('SELECT * FROM v3_funnel WHERE day BETWEEN ? AND ?',(str(start),str(day))) if r['cabinet_id'] in cabinets}
    daily=[];variants=[];index={};variant_catalog=[];barcode_map=defaultdict(set)
    for p in products:
        for variant in p.get('variants',[]):
            for barcode in variant.get('barcodes',[]):
                variant_catalog.append({'cabinet_id':p['cabinet_id'],'nm_id':p['nm_id'],'variant_id':variant.get('variant_id'),'barcode':barcode,'size':variant.get('size'),'source':'Content sizes.chrtID/skus','status':'OK'})
                barcode_map[p['cabinet_id'],p['nm_id'],barcode].add(variant.get('variant_id'))
    for p in products:
        c,n=p['cabinet_id'],p['nm_id']
        created=''
        if p.get('created_at'):
            try:
                dt=datetime.fromisoformat(p['created_at'].replace('Z','+00:00'))
                created=(dt.replace(tzinfo=ZoneInfo('Europe/Moscow')) if dt.tzinfo is None else dt.astimezone(ZoneInfo('Europe/Moscow'))).date().isoformat()
            except ValueError:pass
        p['created_day']=created or None
        for d in dates(start,day):
            ds=str(d); ev=by.get((c,n,ds,'orders'),[])
            cov=coverage.get((c,'orders',ds))
            status='OK' if cov and cov['status']=='OK' else 'PARTIAL' if ev else 'MISSING'
            if created and ds<created:status='NOT_APPLICABLE'
            current=source.get((c,'orders'),{})
            if status=='OK' and current.get('status') is not None and not usable_orders(current):status='STALE'
            units=len(ev) if status not in {'MISSING','NOT_APPLICABLE'} else None
            price_sum=complete_sum(x['order_price'] for x in ev) if ev else 0 if status=='OK' else None
            price_status='OK' if status=='OK' and price_sum is not None else status if status!='OK' else 'PARTIAL'
            f=funnel.get((c,n,ds),{})
            sales=by.get((c,n,ds,'sales'),[])
            sales_ok=coverage.get((c,'sales',ds),{}).get('status')=='OK' and source.get((c,'sales'),{}).get('status')=='OK'
            row={'cabinet_id':c,'nm_id':n,'day':d,'ordered_units':units,'orders_status':status,'known_ordered_units':len(ev),
                 'order_amount':price_sum,'amount_status':price_status,'currency':'RUB','price_type':'ORDER_PRICE_WITH_DISC',
                 'avg_order_price':price_sum/len(ev) if price_sum is not None and ev else None,
                 'buyer_order_amount':complete_sum(x['buyer_price'] for x in ev) if ev else 0 if status=='OK' else None,
                 'cancel_units':sum(bool(x['is_cancel']) for x in ev) if all(x['is_cancel'] is not None for x in ev) and status=='OK' else None,
                 'fbo_units':sum(x['model']=='FBO' for x in ev) if status=='OK' else None,
                 'fbs_units':sum(x['model']=='FBS' for x in ev) if status=='OK' else None,
                 'unknown_model_units':sum(x['model']=='UNKNOWN' for x in ev) if status=='OK' else None,
                 'sales_units':sum(x['event_type']=='sale' for x in sales) if sales_ok else None,
                 'return_units':sum(x['event_type']=='return' for x in sales) if sales_ok else None,
                 'sales_status':'OK' if sales_ok else 'MISSING','order_events':None,'order_events_status':'NOT_SUPPORTED',
                 'orders_source_run_id':cov.get('run_id') if cov else None,
                 'source_day_covered':bool(cov and cov['status']=='OK' and usable_orders(current)),
                 'orders_observed_at':cov.get('observed_at') if cov else None,
                 'funnel_status':f.get('status','MISSING'),'funnel_source_run_id':f.get('source_run_id'),
                 'funnel_observed_at':f.get('observed_at')}
            for k in ('views','carts','funnel_orders','wishlist','funnel_buyout_units'):
                row[k]=f.get(k);row[k+'_status']='OK' if f.get(k) is not None else 'MISSING'
            row['cart_rate']=row['carts']/row['views'] if row['views'] and row['carts'] is not None else None
            row['order_rate']=row['funnel_orders']/row['carts'] if row['carts'] and row['funnel_orders'] is not None else None
            daily.append(row);index[c,n,ds]=row
            grouped=defaultdict(list)
            for e in ev:grouped[e['barcode'],e['size'],e['model']].append(e)
            for (barcode,size,model),group in grouped.items():
                mapped=barcode_map[c,n,barcode]
                variants.append({'cabinet_id':c,'nm_id':n,'barcode':barcode,'size':size,'model':model,'day':d,'ordered_units':len(group),
                                 'variant_id':next(iter(mapped)) if len(mapped)==1 else None,'variant_mapping_status':'OK' if len(mapped)==1 else 'MISSING',
                                 'order_amount':complete_sum(x['order_price'] for x in group),'status':status,'source_run_id':cov.get('run_id') if cov else None,
                                 'zero_policy':'Отсутствующий вариант не означает нулевой остаток/спрос'})

    def values(c,n,lo,hi,field='ordered_units',allow_na=False):
        rows=[index[c,n,str(d)] for d in dates(lo,hi)]
        statuses=[r['orders_status'] if field in {'ordered_units','order_amount'} else r.get(field+'_status','MISSING') for r in rows]
        valid=all(s=='OK' or allow_na and s=='NOT_APPLICABLE' for s in statuses)
        nums=[r[field] if r[field] is not None else 0 if allow_na and s=='NOT_APPLICABLE' else None for r,s in zip(rows,statuses)]
        if field=='order_amount':valid=valid and all(r['amount_status']=='OK' or allow_na and r['orders_status']=='NOT_APPLICABLE' for r in rows)
        return (complete_sum(nums) if valid else None),sum(s=='OK' for s in statuses),len(rows)

    all_products=[];periods=[];evidence=[]
    observations=[]
    for r in store.rows('SELECT * FROM v3_observations ORDER BY snapshot_at'):
        if r['cabinet_id'] in cabinets:observations.append({'cabinet_id':r['cabinet_id'],'source':r['source'],**json.loads(r['payload']),'snapshot_at':r['snapshot_at']})
    # Day-specific snapshot selection never borrows a future snapshot for historical reporting.
    stock_by={}
    period_index={}
    for o in observations:
        if o['source']=='funnel_period':period_index[o['cabinet_id'],o['nm_id'],o['period_start'],o['period_end']]=o
        if o['source'] in {'stocks_wb','stocks_mp','stock_history_wb','stock_history_mp'}:
            if o['snapshot_at'][:10]<=str(day):
                channel=o['source'].replace('stock_history_','stocks_')
                stock_by.setdefault((o['cabinet_id'],o['nm_id'],channel),{})[(o['snapshot_at'],o['source'])]=[]
    for o in observations:
        scope=(o['cabinet_id'],o['nm_id'],o['source'].replace('stock_history_','stocks_')) if o.get('nm_id') else None
        snapshot_key=(o['snapshot_at'],o['source'])
        if scope in stock_by and snapshot_key in stock_by[scope]:stock_by[scope][snapshot_key].append(o)

    for p in products:
        c,n=p['cabinet_id'],p['nm_id']; y=index[c,n,str(day)]
        base_sum,have,expected=values(c,n,day-timedelta(days=14),day-timedelta(days=1))
        raw_base,_,_=values(c,n,day-timedelta(days=14),day-timedelta(days=1),allow_na=True)
        base=base_sum/14 if base_sum is not None else None
        fact=y['ordered_units'] if y['orders_status']=='OK' else None
        delta=fact-base if fact is not None and base is not None else None
        week,_,_=values(c,n,day-timedelta(days=7),day-timedelta(days=1));past,_,_=values(c,n,day-timedelta(days=14),day-timedelta(days=8))
        weekdays=[index[c,n,str(day-timedelta(days=7*i))] for i in range(1,9) if str(day-timedelta(days=7*i))>=str(start)]
        wd=[r['ordered_units'] for r in weekdays if r['orders_status']=='OK']
        weekday_avg=sum(wd)/len(wd) if len(wd)>=4 else None
        duration=0
        if base is not None and base>0:
            for i in range(0,14):
                prev=index[c,n,str(day-timedelta(days=i))]
                if prev['orders_status']=='OK' and prev['ordered_units']<base:duration+=1
                else:break
        stock={};stock_status={}
        for channel in ('stocks_wb','stocks_mp'):
            snaps=stock_by.get((c,n,channel),{})
            chosen=max(snaps) if snaps else None
            stamp=chosen[0] if chosen else None
            rows=snaps.get(chosen,[])
            latest=source.get((c,rows[0]['source'] if rows else channel),{})
            status='OK' if stamp and stamp[:10]==str(day) else 'STALE' if stamp else 'MISSING'
            if latest.get('status') not in {None,'OK'} and stamp:status='STALE'
            stock[channel]=sum(o['quantity'] for o in rows) if rows else None
            stock_status[channel]=status
        stock_total=complete_sum(stock.values()) if all(v=='OK' for v in stock_status.values()) else None
        demand=week/7 if week is not None and week>0 else base if base and base>0 else None
        days_cover=stock_total/demand if stock_total is not None and demand else None
        row={'cabinet_id':c,'nm_id':n,'supplier_article':p.get('supplier_article',''),'title':p.get('title',''),
             'life_status':p.get('life_status','UNKNOWN'),'first_observed_at':p['first_observed_at'],'report_date':day,
             'ordered_units':fact,'known_ordered_units':y['known_ordered_units'],'orders_status':y['orders_status'],
             'base_14_avg':base,'base_14_known_days':have,'base_14_expected_days':expected,
             'delta_units':delta,'change_rate':change(base,fact),'signal':classify(base,fact,have<14),
             'gross_drop':max(-delta,0) if delta is not None else None,'gross_gain':max(delta,0) if delta is not None else None,
             'contribution_base':raw_base/14 if raw_base is not None else None,
             'orders_last_7_sum':week,'orders_previous_7_sum':past,'week_change_rate':change(past,week),
             'weekday_avg':weekday_avg,'weekday_available':len(wd),'weekday_expected':8,
             'weekday_change_rate':change(weekday_avg,fact),'observed_decline_days':duration,
             'decline_first_observed':day-timedelta(days=duration-1) if duration else None,
             'order_amount':y['order_amount'] if y['amount_status']=='OK' else None,'amount_status':y['amount_status'],
             'avg_order_price':y['avg_order_price'],'currency':'RUB',
             'fbo_snapshot_quantity':stock['stocks_wb'],'fbs_snapshot_quantity':stock['stocks_mp'],
             'fbo_status':stock_status['stocks_wb'],'fbs_status':stock_status['stocks_mp'],'stock_quantity':stock_total,
             'stock_coverage_days_estimate':days_cover,'stock_demand_units_per_day':demand,
             'stock_limit':'Последний наблюдавшийся срез не позднее отчётной даты; не непрерывное наличие',
             'wb_url':f'https://www.wildberries.ru/catalog/{n}/detail.aspx',
             'legacy_drop_selected':base is not None and base>0 and fact is not None and (base-fact)/base>threshold/100,
             'priority_absolute':max(-delta,0) if delta is not None else None}
        eco=[e for e in economics if e['cabinet_id']==c and e['nm_id']==n and e['valid_from']<=str(day) and e['status']=='OK']
        eco=max(eco,key=lambda e:e['valid_from']) if eco else None
        row['unit_margin']=eco['unit_margin'] if eco else None;row['margin_status']='OK' if eco else 'MISSING'
        base_rows=[index[c,n,str(d)] for d in dates(day-timedelta(days=14),day-timedelta(days=1))]
        row['base_14_not_applicable_days']=sum(r['orders_status']=='NOT_APPLICABLE' for r in base_rows)
        row['base_14_missing_days']=sum(r['orders_status'] not in {'OK','NOT_APPLICABLE'} for r in base_rows)
        row['alert_selected']=bool(row['legacy_drop_selected'] and base>=pref.get('min_base',0) and -delta>=pref.get('min_absolute',0) and duration>=pref.get('repeat_days',1))
        if row['signal']=='РЕДКИЙ СПРОС' and pref.get('rare_mode')=='hide':row['alert_selected']=False
        for days in (7,14,30):
            curlo=day-timedelta(days=days);curhi=day-timedelta(days=1);oldlo=day-timedelta(days=2*days);oldhi=curlo-timedelta(days=1)
            cur,ch,ce=values(c,n,curlo,curhi);old,oh,oe=values(c,n,oldlo,oldhi)
            money,_,_=values(c,n,curlo,curhi,'order_amount');oldmoney,_,_=values(c,n,oldlo,oldhi,'order_amount')
            periods.append({'cabinet_id':c,'nm_id':n,'window_days':days,'period_start':curlo,'period_end':curhi,'previous_start':oldlo,'previous_end':oldhi,
                            'units':cur,'previous_units':old,'avg_units':cur/days if cur is not None else None,'previous_avg_units':old/days if old is not None else None,
                            'change_rate':change(old,cur),'order_amount':money,'previous_order_amount':oldmoney,'amount_delta':money-oldmoney if money is not None and oldmoney is not None else None,
                            'available_days':ch,'expected_days':ce,'previous_available_days':oh,'previous_expected_days':oe,
                            'status':'OK' if cur is not None and old is not None else 'PARTIAL'})
        # No more than 3 evidence-backed hypotheses per product.
        rules=[]
        if row['orders_status']=='OK' and base is None and p.get('created_day') and p['created_day']>str(day-timedelta(days=14)):
            rules.append(('NEW_PRODUCT','Наблюдать новый товар','Новая карточка: полная 14-дневная применимая база ещё не накоплена','причина не установлена','Наблюдать первые завершённые дни; не сравнивать с искусственной полной нулевой базой','Доступная история с даты создания',None))
        elif row['orders_status']!='OK' or base is None:
            rules.append(('QUALITY_ORDERS','Проверить данные','Данных недостаточно для полного сравнения','причина не установлена','Восстановить загрузку и проверить число доступных дней','Полное покрытие периода',None))
        elif delta is not None and delta<0 and base<1:
            rules.append(('RARE_DEMAND','Наблюдать спрос','Редкий спрос: процент нестабилен','причина не установлена','Сравнить абсолютное изменение и повторяемость за 7 дней','Повторяемость на завершённых днях',None))
        elif delta is not None and delta<0:
            rules.append(('DECLINE','Проверить ограничение','Заказанные единицы ниже собственного 14-дневного ориентира','причина не установлена','Проверить наличие, цену, трафик и доставку по данным кабинета','Заказанные единицы и качество данных',None))
        if fact is not None and base is not None and fact>base and base>0 and change(base,fact)>=pref.get('growth_threshold',.15):
            rules.append(('OBSERVED_GROWTH','Проверить возможность роста','Наблюдаемый рост заказанных единиц','гипотеза','Проверить повторяемость, запас и экономику перед тестом продвижения','Рост за 7 дней без ухудшения подтверждённой маржи',None))
            if eco and eco['unit_margin']>0 and stock_total and week is not None and past is not None and week>past:
                rules[-1]=('OBSERVED_GROWTH','Проверить возможность роста','Рост дня и недели совпадает; запас и положительная удельная маржа подтверждены указанными источниками','гипотеза поддержана несколькими наблюдениями','Рассмотреть ограниченный тест продвижения; заранее задать бюджет, проверку расходов и стоп при ухудшении подтверждённой экономики','Единицы, фактические расходы и маржа в сопоставимом периоде',None)
        if days_cover is not None and days_cover<pref.get('stock_days',7):
            rules.append(('LOW_COVER','Устранить ограничение продаж','Запас мал относительно наблюдаемой скорости','гипотеза','Проверить сроки пополнения, размеры и склады','Покрытие запасом и отсутствие пропусков',None))
        first_price=values(c,n,day-timedelta(days=14),day-timedelta(days=8),'order_amount')[0]
        last_price=values(c,n,day-timedelta(days=7),day-timedelta(days=1),'order_amount')[0]
        if first_price is not None and last_price is not None and past and week and first_price/past<last_price/week and delta is not None and delta<0:
            rules.append(('PRICE_COINCIDENCE','Проверить ограничение','Средняя цена заказов выросла одновременно со снижением заказов','гипотеза','Проверить смену состава вариантов и трафик; затем спланировать сопоставимый тест','Цена и конверсия при сопоставимых условиях',None))
        # Traffic/conversion hypotheses require complete identical daily Analytics periods.
        traffic=[]
        for lo,hi in [(day-timedelta(days=14),day-timedelta(days=8)),(day-timedelta(days=7),day-timedelta(days=1))]:
            vals={field:values(c,n,lo,hi,field)[0] for field in ('views','carts','funnel_orders')}
            if any(v is None for v in vals.values()):
                aggregate=period_index.get((c,n,str(lo),str(hi)))
                if aggregate:
                    vals={field:aggregate.get(field) for field in ('views','carts','funnel_orders')}
            traffic.append(vals)
        a,b=traffic
        if all(v is not None for x in traffic for v in x.values()) and a['views']>0 and a['carts']>0 and b['views']>0 and b['carts']>0:
            if b['views']>=a['views']*.9 and b['funnel_orders']/b['carts']<a['funnel_orders']/a['carts']:
                scope_ok=any(x['cabinet_id']==c and x['nm_id']==n and x['period_start']<=str(day-timedelta(days=14)) and x['period_end']>=str(day-timedelta(days=1)) and x['availability_status']=='CONFIRMED_AVAILABLE' and x['status']=='OK' for x in availability)
                scenario=b['carts']*a['funnel_orders']/a['carts']-b['funnel_orders'] if scope_ok else None
                rules.insert(0,('CONVERSION_RESTORE','Проверить возможность роста','Трафик сохраняется, отношение заказов к корзинам ниже','гипотеза','Проверить цену, доставку, отзывы; тестировать одну гипотезу','Отношение сумм заказов/корзин Analytics',{'scenario_units':scenario,'horizon_days':7,'method':'last_carts * previous_orders / previous_carts - last_orders','inputs':{**{'previous_'+k:v for k,v in a.items()},**{'last_'+k:v for k,v in b.items()}},'kind':'RESTORATION','constraint':'Наличие в базовом периоде и маржа не подтверждены; арифметический сценарий, не прогноз'}))
            elif b['views']<a['views']*.9 and b['funnel_orders']/b['carts']>=a['funnel_orders']/a['carts']:
                rules.insert(0,('TRAFFIC_CHECK','Проверить возможность роста','Охват снизился при устойчивом отношении заказов к корзинам','гипотеза','Проверить поиск и рекламу, наличие и маржу','Трафик и согласованные отношения событий',None))
        for rule,category,fact_text,explanation,action,success,scenario in rules[:3]:
            if scenario:
                scenario['constraint']=('Доступность подтверждена ручным источником в заявленной области; маржа/бюджет и переносимость условий не подтверждены. Арифметический сценарий, не прогноз.' if scenario['scenario_units'] is not None else 'Возможность обнаружена, объём не оценён: сопоставимая доступность не подтверждена. Нужны данные AvailabilityPeriods; маржа и бюджет отдельно.')
            before_value,after_value,metric_id,unit=base,fact,'ordered_units','шт./день'
            if rule=='CONVERSION_RESTORE':before_value,after_value,metric_id,unit=a['funnel_orders']/a['carts'],b['funnel_orders']/b['carts'],'order_rate','доля'
            if rule=='TRAFFIC_CHECK':before_value,after_value,metric_id,unit=a['views'],b['views'],'views','события'
            if rule=='PRICE_COINCIDENCE':before_value,after_value,metric_id,unit=first_price/past,last_price/week,'avg_order_price','RUB'
            if rule=='LOW_COVER':before_value,after_value,metric_id,unit=None,days_cover,'stock_coverage_days_estimate','дней, оценка'
            evidence.append({'evidence_id':key(rule,c,n,str(day)),'rule_id':rule,'rule_version':'1.0','cabinet_id':c,'nm_id':n,
                             'period_start':day-timedelta(days=14),'period_end':day,'first_observed':day,'last_observed':day,
                             'observation_status':'подтверждённый факт источника' if row['orders_status']=='OK' else 'частичные данные',
                             'explanation_status':explanation,'category':category,'fact':fact_text,'base_value':before_value,'fact_value':after_value,'metric_id':metric_id,'unit':unit,
                             'evidence_refs':f'ProductDaily:{c}/{n}/{day-timedelta(days=14)}..{day};FunnelPeriods:{c}/{n}/exact_period/latest_snapshot;SourceStatus:{c}/orders,funnel_period,funnel',
                             'scope':'карточка; все подтверждённые модели + UNKNOWN отдельно','contradictions':'Причинный эффект не проверялся',
                             'missing_checks':'Маржа, сопоставимое наличие, доставка, атрибуция рекламы; см. MetricRegistry',
                             'next_action':action,'metric_success':success,'review_date':day+timedelta(days=7),
                             'scenario_kind':scenario.get('kind') if scenario else None,'scenario_units':scenario.get('scenario_units') if scenario else None,
                             'scenario_method':scenario.get('method') if scenario else None,'scenario_inputs':json.dumps(scenario.get('inputs'),ensure_ascii=False) if scenario else None,
                             'scenario_horizon_days':scenario.get('horizon_days') if scenario else None,'scenario_limits':scenario.get('constraint') if scenario else 'Возможность обнаружена, объём не оценён',
                             'scenario_stop':'Остановить тест при ухудшении подтверждённой экономики, дефиците или нарушении сопоставимости',
                             'scenario_additive':False})
        all_products.append(row)

    summaries=[]
    for c in cabinets:
        subset=[r for r in all_products if r['cabinet_id']==c]
        have=sum(coverage.get((c,'orders',str(d)),{}).get('status')=='OK' for d in dates(day-timedelta(days=14),day))
        current=source.get((c,'orders'),{})
        complete=have==15 and usable_orders(current)
        def amount(ds,field):
            ev=[e for e in events if e['cabinet_id']==c and e['source']=='orders' and e['day'] in ds]
            return len(ev) if field=='units' else complete_sum(e['order_price'] for e in ev) if ev else 0
        baseline_days={str(d) for d in dates(day-timedelta(days=14),day-timedelta(days=1))}
        factual=amount({str(day)},'units');base=amount(baseline_days,'units')/14
        drop=sum(max((r['contribution_base'] or 0)-(r['ordered_units'] or 0),0) for r in subset if r['contribution_base'] is not None and r['ordered_units'] is not None)
        gain=sum(max((r['ordered_units'] or 0)-(r['contribution_base'] or 0),0) for r in subset if r['contribution_base'] is not None and r['ordered_units'] is not None)
        summaries.append({'cabinet_id':c,'report_date':day,'ordered_units':factual if complete else None,'known_ordered_units':factual,
                          'base_14_avg':base if complete else None,'known_base_14_subtotal':base,'delta_units':factual-base if complete else None,
                          'change_rate':change(base,factual) if complete else None,'gross_drop':drop if complete else None,'gross_gain':gain if complete else None,
                          'order_amount':amount({str(day)},'money') if complete else None,'base_order_amount_avg':(amount(baseline_days,'money')/14 if amount(baseline_days,'money') is not None else None) if complete else None,
                          'products':len(subset),'available_days':have,'expected_days':15,'status':'OK' if complete else 'PARTIAL',
                          'source_phase':current.get('phase'),'source_pages':current.get('pages',0),'source_records':current.get('records',0),'last_success_at':current.get('last_success_at') or (current.get('completed_at') if current.get('status')=='OK' else None),
                          'reason':source.get((c,'orders'),{}).get('reason','Нет успешной загрузки Statistics'),
                          'reconciliation_delta':(gain-drop)-(factual-base) if complete else None})
        same_weekdays=[day-timedelta(days=7*i) for i in range(1,9)]
        available_weekdays=[d for d in same_weekdays if coverage.get((c,'orders',str(d)),{}).get('status')=='OK']
        wd_base=sum(amount({str(d)},'units') for d in available_weekdays)/len(available_weekdays) if len(available_weekdays)>=4 and complete else None
        summaries[-1].update(weekday_avg=wd_base,weekday_available=len(available_weekdays),weekday_expected=8,
                             weekday_change_rate=change(wd_base,factual) if complete else None,
                             orders_last_7_sum=amount({str(d) for d in dates(day-timedelta(days=7),day-timedelta(days=1))},'units') if complete else None,
                             orders_previous_7_sum=amount({str(d) for d in dates(day-timedelta(days=14),day-timedelta(days=8))},'units') if complete else None)
        s=summaries[-1];s['week_change_rate']=change(s['orders_previous_7_sum'],s['orders_last_7_sum'])
        s['amount_delta']=s['order_amount']-s['base_order_amount_avg'] if s['order_amount'] is not None and s['base_order_amount_avg'] is not None else None
        if s['reconciliation_delta'] is not None and abs(s['reconciliation_delta'])>1e-8:
            s.update(status='PARTIAL',reason='Не сошлось разложение прироста/спада с итогом: проверить жизненный статус и записи источника')
        for r in subset:
            r['share_gross_drop']=r['gross_drop']/drop if r['gross_drop'] is not None and drop>0 else None
            r['share_gross_gain']=r['gross_gain']/gain if r['gross_gain'] is not None and gain>0 else None
    manual=defaultdict(list)
    for r in store.rows('SELECT * FROM v3_manual'):
        manual[r['kind']].append(json.loads(r['payload']))
    comparisons=[]
    groups=defaultdict(list)
    for m in manual['ProductMapping']:
        if m.get('mapping_status')=='CONFIRMED':groups[m['canonical_product_id']].append(m)
    lookup={(r['cabinet_id'],r['nm_id']):r for r in all_products}
    for canonical,mappings in groups.items():
        if len(mappings)!=2 or len({m['cabinet_id'] for m in mappings})!=2:continue
        rows=[lookup.get((m['cabinet_id'],m['nm_id'])) for m in mappings]
        if not all(rows) or len({m['comparison_unit'] for m in mappings})!=1:continue
        if any(m.get('variant_id') not in {'','CARD'} for m in mappings):
            comparisons.append({'canonical_product_id':canonical,'report_date':day,'status':'NOT_SUPPORTED','reason':'Сопоставление варианта подтверждено, но заказы не связаны с chrtID; итог карточки не копируется на вариант'})
            continue
        fact=complete_sum(r['ordered_units']*m['units_per_item'] if r['ordered_units'] is not None else None for r,m in zip(rows,mappings))
        base=complete_sum(r['base_14_avg']*m['units_per_item'] if r['base_14_avg'] is not None else None for r,m in zip(rows,mappings))
        comparisons.append({'canonical_product_id':canonical,'comparison_unit':mappings[0]['comparison_unit'],'fact_units':fact,'base_units':base,'delta_units':fact-base if fact is not None and base is not None else None,
                            'change_rate':change(base,fact),'cabinet_a':mappings[0]['cabinet_id'],'nm_a':mappings[0]['nm_id'],'cabinet_b':mappings[1]['cabinet_id'],'nm_b':mappings[1]['nm_id'],
                            'mapping_source':mappings[0]['mapping_source'],'status':'OK' if fact is not None and base is not None else 'PARTIAL','limit':'Перераспределение заказов не является ростом пары; не контрольная группа'})
        comparisons[-1].update(report_date=day,ordered_units_a=rows[0]['ordered_units'],ordered_units_b=rows[1]['ordered_units'],
                               change_rate_a=rows[0]['change_rate'],change_rate_b=rows[1]['change_rate'],
                               avg_order_price_per_unit_a=rows[0]['avg_order_price']/mappings[0]['units_per_item'] if rows[0]['avg_order_price'] is not None else None,
                               avg_order_price_per_unit_b=rows[1]['avg_order_price']/mappings[1]['units_per_item'] if rows[1]['avg_order_price'] is not None else None,
                               stock_comparison_units_a=rows[0]['stock_quantity']*mappings[0]['units_per_item'] if rows[0]['stock_quantity'] is not None else None,
                               stock_comparison_units_b=rows[1]['stock_quantity']*mappings[1]['units_per_item'] if rows[1]['stock_quantity'] is not None else None,
                               stock_status_a='OK' if rows[0]['stock_quantity'] is not None else 'MISSING',stock_status_b='OK' if rows[1]['stock_quantity'] is not None else 'MISSING',
                               price_definition='Средняя priceWithDisc среди заказов / подтверждённая комплектность, не цена предложения',
                               delivery_funnel_ads_limit='Подробные одинаковые периоды — FunnelPeriods/AdsDaily по каждой nm_id; неполные показатели не подменены нулём')
    if save:store.record_evidence(evidence)
    actions=[]
    for r in store.rows('SELECT * FROM v3_actions'):
        payload=json.loads(r['payload'])
        if payload.get('cabinet_id') in cabinets:actions.append({**{k:v for k,v in r.items() if k!='payload'},**payload})
    tables={'Products':products,'ProductDaily':daily,'VariantDaily':variants,'PeriodMetrics':periods,
            'CabinetComparison':comparisons,
            'VariantCatalog':variant_catalog,
            'PriceHistory':[o for o in observations if o['source']=='prices'],
            'StockSnapshots':[o for o in observations if o['source'] in {'stocks_wb','stocks_mp','stock_history_wb','stock_history_mp'}],
            'SearchDaily':[o for o in observations if o['source']=='search'],
            'FunnelPeriods':[o for o in observations if o['source']=='funnel_period'],
            'CardMetricSnapshots':[o for o in observations if o['source']=='card_metrics'],
            'CardEvents':[o for o in observations if o['source']=='card_events'],
            'Evidence':evidence,'Actions':actions,'SourceStatus':list(source.values()),'MetricRegistry':registry(cabinets,list(source.values()))}
    referenced={r.get('orders_source_run_id') for r in daily}|{r.get('funnel_source_run_id') for r in daily}|{e.get('source_run_id') for e in events}|{o.get('run_id') for o in observations}
    lineage=[s for s in store.sources() if s['cabinet_id'] in cabinets and s['run_id'] in referenced]
    all_status={(s['run_id'],s['cabinet_id'],s['source']):s for s in lineage}
    for s in source.values():all_status[s['run_id'],s['cabinet_id'],s['source']]=s
    tables['SourceStatus']=[{**s,'is_latest':source.get((s['cabinet_id'],s['source']),{}).get('run_id')==s['run_id']} for s in all_status.values()]
    for name in TABLES:tables.setdefault(name,manual.get(name,[]))
    api_ads={}
    for o in observations:
        if o['source']=='ads':api_ads[o['cabinet_id'],o['observation_key']]=o
    if api_ads:
        if tables['AdsDaily']:
            tables['AdsDaily_Manual']=tables['AdsDaily']
        tables['AdsDaily']=list(api_ads.values())
    tables['CampaignSnapshots']=[o for o in observations if o['source']=='campaigns']
    campaign_events=[];prior_campaign={}
    for snapshot in tables['CampaignSnapshots']:
        identity=(snapshot['cabinet_id'],snapshot['campaign_id'])
        before=prior_campaign.get(identity)
        if before and before['campaign_status']!=snapshot['campaign_status']:
            campaign_events.append({'event_id':key(identity,snapshot['snapshot_at']),'cabinet_id':identity[0],'campaign_id':identity[1],
                                    'before_status':before['campaign_status'],'after_status':snapshot['campaign_status'],'observed_at':snapshot['snapshot_at'],
                                    'source_change_time':snapshot.get('source_change_time'),'status':'OK','explanation_status':'причина не установлена',
                                    'limit':'Факт изменения статуса кампании; влияние на заказы не оценено'})
        prior_campaign[identity]=snapshot
    tables['CampaignEvents']=campaign_events
    if api_ads and not tables['CampaignProducts']:
        seen=set()
        for ad in api_ads.values():
            identity=(ad['cabinet_id'],ad['campaign_id'],ad['nm_id'])
            if identity not in seen:
                seen.add(identity);tables['CampaignProducts'].append({'cabinet_id':ad['cabinet_id'],'campaign_id':ad['campaign_id'],'nm_id':ad['nm_id'],'valid_from':ad['day'],'source':'Наблюдалось в статистике WB','status':'PARTIAL','reason':'Только наблюдавшиеся товары, не полный состав кампании'})
    for name in ('AdsDaily','CampaignProducts','Economics','CompetitorSnapshots','ProductMapping'):
        if manual.get(name):
            for c in cabinets:
                subset=[r for r in manual[name] if r.get('cabinet_id',c)==c]
                if subset:
                    channel={'AdsDaily':'ads_import','Economics':'margin','CompetitorSnapshots':'competitors','ProductMapping':'product_mapping'}.get(name,name)
                    tables['SourceStatus'].append({'run_id':'manual-import','cabinet_id':c,'source':channel,'status':'OK' if all(r.get('status','OK')=='OK' for r in subset) else 'PARTIAL','reason':'Подтверждённый ручной импорт; исходный источник и период указаны в строках','records':len(subset),'requested_at':None,'completed_at':None,'snapshot_at':None,'pages':0})
    tables['MetricRegistry']=registry(cabinets,tables['SourceStatus'])
    for c in cabinets:
        latest_stock={}
        for o in observations:
            if o['cabinet_id']==c and o['source'] in {'stocks_wb','stocks_mp'}:
                latest_stock[o['source'],o['observation_key']]=o
        if len(latest_stock)>=20 and sum(o['quantity']==0 for o in latest_stock.values())/len(latest_stock)>.95:
            tables['SourceStatus'].append({'run_id':'derived-quality','cabinet_id':c,'source':'QUALITY_MASS_ZERO_STOCKS','status':'PARTIAL','reason':'Более 95% наблюдаемых остатков нулевые. Сверить время, источники и ассортимент; нули не объявлены автоматически ошибкой или дефицитом.','records':len(latest_stock)})
    # Explicitly dated offer snapshots, with their age; no invented historical offer prices.
    price_groups=defaultdict(list)
    for o in tables['PriceHistory']:
        price_groups[o['cabinet_id'],o['nm_id'],o['size_id'],o['price_type']].append(o)
    price_comparisons=[]
    for (c,n,size,kind),rows in price_groups.items():
        for lag in (0,1,3,7,14,30):
            target=day-timedelta(days=lag)
            candidates=[o for o in rows if o['snapshot_at'][:10]<=str(target)]
            snapshot=max(candidates,key=lambda x:x['snapshot_at']) if candidates else None
            age=(target-date.fromisoformat(snapshot['snapshot_at'][:10])).days if snapshot else None
            price_comparisons.append({'cabinet_id':c,'nm_id':n,'size_id':size,'price_type':kind,'requested_day':target,'lag_days':lag,
                                      'value':snapshot.get('value') if snapshot else None,'snapshot_at':snapshot['snapshot_at'] if snapshot else None,'age_days':age,
                                      'status':'OK' if snapshot and age==0 else 'STALE' if snapshot else 'MISSING','currency':snapshot.get('currency') if snapshot else None})
    tables['PriceComparisons']=price_comparisons
    # Advertising ratios preserve attribution; no "organic = all - attributed" calculation.
    for ad in tables['AdsDaily']:
        ad['ctr']=ad['clicks']/ad['impressions'] if ad.get('impressions') and ad.get('clicks') is not None else None
        ad['cpc']=ad['spend']/ad['clicks'] if ad.get('clicks') and ad.get('spend') is not None else None
        ad['drr']=ad['spend']/ad['attributed_amount'] if ad.get('attributed_amount') and ad.get('spend') is not None else None
        overall=sum(r['known_ordered_units'] for r in daily if r['cabinet_id']==ad['cabinet_id'] and r['nm_id']==ad['nm_id'] and str(r['day'])==str(ad['day']))
        ad['attribution_check']='CHECK_WINDOW_AND_SCOPE' if ad.get('attributed_orders') is not None and ad['attributed_orders']>overall else 'NOT_RECONCILED_DIFFERENT_DEFINITIONS'
    for a in actions:
        completed=a.get('completed_at')
        if completed:
            done=date.fromisoformat(completed[:10]);c,n=a['cabinet_id'],a['nm_id']
            before=[index.get((c,n,str(done-timedelta(days=i)))) for i in range(1,8)]
            after=[index.get((c,n,str(done+timedelta(days=i)))) for i in range(1,8)]
            if all(r and r['orders_status']=='OK' for r in before+after):
                a['before_units']=sum(r['ordered_units'] for r in before);a['after_units']=sum(r['ordered_units'] for r in after)
                a['observed_after_change']=a['after_units']-a['before_units'];a['result_status']='OK'
            else:a['result_status']='MISSING'
            a['result_limits']='7 завершённых дней до/после ручной отметки; совпадение не доказывает эффект действия'
    payload={'schema_version':'3.0','report_version':VERSION,'timezone':'Europe/Moscow','report_date':str(day),'generated_at':now(),'run_id':run,
             'cabinets':cabinets,'summary':summaries,'all_products':all_products,'comparisons':comparisons,'tables':tables,
             'intervals':{'baseline_start':str(day-timedelta(days=14)),'baseline_end':str(day-timedelta(days=1)),'target_history_start':str(target_start),'target_history_end':str(day),'exported_history_start':str(start),'exported_history_end':str(day)},
             'limitations':['История ограничена фактическим покрытием SourceStatus; цель 90 дней, ранее накопленные данные сохраняются.',
                            'Заказы Statistics предварительные и не покрывают некоторые способы оплаты. Полный итог относится к этому контракту.',
                            'История цен/остатков начинается с первого снимка; будущие снимки не переносятся в прошлое.',
                            'Заказы как многотоварные события и общие показы не получены. Маржа и конкуренты требуют подтверждённого импорта; окно атрибуции рекламы требует проверки.',
                            'Не подключены платные услуги. Сценарии не прогноз, не суммируются автоматически.'],
             'status':'OK' if summaries and all(s['status']=='OK' for s in summaries) else 'PARTIAL'}
    return store.save_report(run,day,payload) if save else {**payload,'revision':0}


def telegram(report,names):
    from html import escape
    def num(x):return 'Н/Д' if x is None else f'{x:,.2f}'.rstrip('0').rstrip('.')
    lines=[f'<b>WB: {report["report_date"]}</b> · v{report["report_version"]} r{report["revision"]}',
           'Сформировано: '+datetime.fromisoformat(report['generated_at']).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Moscow')).strftime('%d.%m %H:%M МСК')]
    if report['status']!='OK':lines.append('<b>Итог неполный. Ниже известные подытоги и ограничения.</b>')
    if any(s.get('source_phase') in {'RUNNING','QUEUED'} and not s.get('last_success_at') for s in report['summary']):
        lines.append('Идёт первая загрузка. 0 подтверждённых дней означает, что окончание выгрузки ещё не подтверждено, а не отсутствие заказов. Дополнительные источники могут ожидать очереди. /quality — прогресс; /overview — обновлённый обзор.')
    for s in report['summary']:
        if s.get('source_phase')=='WAITING_RETRY':
            lines.append(escape(s['reason']))
        if s.get('source_phase') in {'RUNNING','QUEUED'}:
            lines.append(f'Загрузка: {s.get("source_pages",0)} стр., {s.get("source_records",0)} строк.'+(' Показан ранее подтверждённый срез; обновление ещё выполняется.' if s.get('last_success_at') else ' Подытоги пока не являются полными.'))
        lines+=[f'<b>{escape(names.get(s["cabinet_id"],s["cabinet_id"]))}</b>',
                f'Единицы: {num(s["ordered_units"])}; известный подытог {s["known_ordered_units"]}. База/день: {num(s["base_14_avg"])}.',
                f'Изменение: {num(s["delta_units"])} шт.; '+(f'{s["change_rate"]:+.1%}' if s['change_rate'] is not None else 'Н/Д'),
                f'Сумма заказов: {num(s["order_amount"])} ₽. Покрытие: {s["available_days"]}/{s["expected_days"]} дней ({s["status"]}).']
        lines.append(f'База по дню недели: {num(s["weekday_avg"])} шт. ({s["weekday_available"]}/{s["weekday_expected"]} дней). Неделя к неделе: '+(f'{s["week_change_rate"]:+.1%}' if s['week_change_rate'] is not None else 'Н/Д'))
    if report['status']=='OK':
        fact=sum(s['ordered_units'] for s in report['summary']);base=sum(s['base_14_avg'] for s in report['summary'])
        lines.append(f'<b>Все выбранные кабинеты:</b> {fact} шт., к базе {num(base)}; '+(f'{change(base,fact):+.1%}' if base>0 else 'нет процентной базы'))
        lines.append(f'Валовой спад {num(sum(s["gross_drop"] for s in report["summary"]))}; прирост {num(sum(s["gross_gain"] for s in report["summary"]))}.')
    drop=sorted([r for r in report['all_products'] if r['alert_selected']],key=lambda r:r['priority_absolute'] or 0,reverse=True)[:3]
    if drop:lines.append('<b>Проверить в первую очередь:</b>')
    for r in drop:lines.append(f'• {escape(r["supplier_article"] or r["nm_id"])}: {num(r["delta_units"])} шт.; {escape(r["signal"])}.')
    grow=[e for e in report['tables']['Evidence'] if e['category']=='Проверить возможность роста'][:3]
    if grow:lines.append('<b>Возможности для проверки:</b>')
    for e in grow:lines.append(f'• {escape(e["nm_id"])}: {escape(e["fact"])}. {escape(e["next_action"])}.')
    if not grow:lines.append('Возможности роста: подтверждённых оснований пока недостаточно; проверьте качество и накопление истории.')
    lines.append('Факты источника и гипотезы разделены. Полные данные и определения — в аналитическом пакете.')
    return '\n'.join(lines)[:3900]

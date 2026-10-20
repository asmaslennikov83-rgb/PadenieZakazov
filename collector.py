"""Read-only adapters and resumable, bounded synchronization."""
from __future__ import annotations
import asyncio
from datetime import date, datetime, timedelta
import json
import os
import time
import uuid
from zoneinfo import ZoneInfo
import aiohttp
from config import CABINETS
from data_quality import nm_id, number, count
from history_store import Store, now, key, dumps
from stock_history_client import StockHistoryError
from analytics_client import AnalyticsError

MSK=ZoneInfo('Europe/Moscow')


class SourceError(RuntimeError):
    def __init__(self, status, reason):
        self.status=status
        super().__init__(reason)


def event_day(value):
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (dt.replace(tzinfo=MSK) if dt.tzinfo is None else dt.astimezone(MSK)).date().isoformat()
    except (ValueError,TypeError):
        raise SourceError('PARTIAL','Не распознана дата события')


class ReadOnlyHTTP:
    ALLOWED={
      ('GET','https://statistics-api.wildberries.ru/api/v1/supplier/orders'),
      ('GET','https://statistics-api.wildberries.ru/api/v1/supplier/sales'),
      ('GET','https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter'),
      ('POST','https://content-api.wildberries.ru/content/v2/get/cards/list'),
      ('POST','https://content-api.wildberries.ru/content/v2/get/cards/trash'),
      ('POST','https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses'),
      ('POST','https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/seller-warehouses'),
      ('POST','https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history'),
      ('POST','https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products'),
      ('POST','https://seller-analytics-api.wildberries.ru/api/v2/search-report/table/details'),
      ('GET','https://advert-api.wildberries.ru/adv/v1/promotion/count'),
      ('GET','https://advert-api.wildberries.ru/adv/v3/fullstats'),
    }
    def __init__(self):
        self.last={}
        self.locks={}

    async def request(self,cabinet,source,method,url,token,interval=21,**kwargs):
        if (method,url) not in self.ALLOWED: raise ValueError('Не разрешён метод read-only адаптера')
        if not token: raise SourceError('MISSING','Токен не настроен; права не проверены')
        lock=self.locks.setdefault((cabinet,source),asyncio.Lock())
        async with lock:
            for attempt in range(3):
                wait=max(0,interval-(time.monotonic()-self.last.get((cabinet,source),0)))
                await asyncio.sleep(wait)
                self.last[cabinet,source]=time.monotonic()
                try:
                    timeout=aiohttp.ClientTimeout(total=45)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.request(method,url,headers={'Authorization':token},**kwargs) as response:
                            if response.status==204: return None
                            if response.status==200:
                                try: return await response.json(content_type=None)
                                except (ValueError,UnicodeDecodeError): raise SourceError('ERROR','Ответ не является JSON')
                            if response.status in {401,403}: raise SourceError('NO_ACCESS',f'HTTP {response.status}: проверьте категорию токена и кабинет')
                            if response.status==402: raise SourceError('NO_ACCESS','HTTP 402: проверьте условия доступа/подписку; автоматически не подключается')
                            if response.status==429 and attempt<2:
                                try: delay=float(response.headers.get('Retry-After','65'))
                                except ValueError: delay=65
                                # Never hold the report indefinitely for a Base-token limit.
                                if delay>120: raise SourceError('ERROR',f'HTTP 429: повтор допустим через {delay:g} секунд; отложите синхронизацию')
                                await asyncio.sleep(max(interval,delay,2**attempt))
                                continue
                            if response.status>=500 and attempt<2:
                                await asyncio.sleep(2**(attempt+1)); continue
                            raise SourceError('ERROR',f'HTTP {response.status}; метод {source}, без содержимого ответа и секретов')
                except (aiohttp.ClientError,asyncio.TimeoutError):
                    if attempt==2: raise SourceError('ERROR','Ошибка соединения/таймаут WB')
                    await asyncio.sleep(2**(attempt+1))


class Collector:
    def __init__(self,store:Store,http=None):
        self.store=store
        self.http=http or ReadOnlyHTTP()

    def token(self,cabinet,category):
        suffix={'spb_tt':'SPB_TT','spb_tt_group':'SPB_TT_GROUP'}[cabinet]
        if category=='statistics': return CABINETS[cabinet].token
        return os.getenv(f'WB_{category.upper()}_TOKEN_{suffix}','').strip() or (CABINETS[cabinet].analytics_token if category=='analytics' else '') or CABINETS[cabinet].token

    async def sync_source(self,run,cabinet,source,operation,budget=150):
        started=now()
        meta={'status':'PARTIAL','reason':'Синхронизация выполняется','requested_at':started,'period_start':None,'period_end':None,'pages':0,'records':0,'cursor':None,'snapshot_at':None}
        self.store.source(run,cabinet,source,**meta)
        try:
            async with asyncio.timeout(budget):
                await operation(meta)
            meta.update(status='OK',reason='Получено по контракту источника')
        except SourceError as exc:
            meta.update(status='PARTIAL' if meta['records'] else exc.status,reason=str(exc))
        except (StockHistoryError,AnalyticsError) as exc:
            status='NO_ACCESS' if any(code in str(exc) for code in ('HTTP 401','HTTP 402','HTTP 403')) else 'ERROR'
            meta.update(status='PARTIAL' if meta['records'] else status,reason=str(exc))
        except asyncio.TimeoutError:
            meta.update(status='PARTIAL' if meta['records'] else 'ERROR',reason='Истёк бюджет загрузки; данные не объявлены полными')
        except asyncio.CancelledError:
            meta.update(status='PARTIAL',reason='Загрузка прервана остановкой процесса; курсор сохранён')
            self.store.source(run,cabinet,source,**meta)
            raise
        except Exception as exc:
            meta.update(status='PARTIAL' if meta['records'] else 'ERROR',reason=f'Ошибка адаптера {type(exc).__name__}; ответ не экспортируется')
        previous=[x for x in self.store.sources() if x['cabinet_id']==cabinet and x['source']==source and x.get('status')=='OK']
        meta['last_success_at']=meta.get('last_success_at') if meta.get('cache_hit') else now() if meta['status']=='OK' else previous[-1].get('completed_at') if previous else None
        meta['completed_at']=now()
        self.store.source(run,cabinet,source,**meta)
        return meta

    async def sync(self,cabinets,extras=False):
        run=uuid.uuid4().hex
        async def cabinet_sync(cabinet):
            owner=self.store.lease('sync:'+cabinet,1800)
            if not owner: return
            try:
                await self.sync_source(run,cabinet,'orders',lambda m:self.ledger(run,cabinet,'orders',m))
                if extras:
                    for source,fn in [('sales',lambda m:self.ledger(run,cabinet,'sales',m)),
                                      ('catalog',lambda m:self.catalog(run,cabinet,m)),
                                      ('prices',lambda m:self.prices(run,cabinet,m)),
                                      ('stocks_wb',lambda m:self.stocks(run,cabinet,'wb',m)),
                                      ('stocks_mp',lambda m:self.stocks(run,cabinet,'mp',m)),
                                      ('funnel_period',lambda m:self.funnel_period(run,cabinet,m)),
                                      ('funnel',lambda m:self.funnel(run,cabinet,m))]:
                        await self.sync_source(run,cabinet,source,fn,budget=int(os.getenv('WB_OPTIONAL_BUDGET_SECONDS','90')))
                    if os.getenv('WB_ENABLE_SEARCH','false').lower()=='true':
                        await self.sync_source(run,cabinet,'search',lambda m:self.search(run,cabinet,m))
                    await self.sync_source(run,cabinet,'ads',lambda m:self.ads(run,cabinet,m))
                    if os.getenv('WB_ENABLE_STOCK_HISTORY','true').lower()=='true':
                        from historical_stock_adapter import collect
                        end=datetime.now(MSK).date()-timedelta(days=1)
                        for kind in ('wb','mp'):
                            await self.sync_source(run,cabinet,'stock_history_'+kind,lambda m,k=kind:collect(self.store,cabinet,k,run,end,m),budget=150)
            finally:
                self.store.release('sync:'+cabinet,owner)
        await asyncio.gather(*(cabinet_sync(c) for c in cabinets))
        self.store.prune_diagnostics()
        return run

    async def ledger(self,run,cabinet,source,meta):
        today=datetime.now(MSK).date()
        state=self.store.sync_state(cabinet,source)
        start=date.fromisoformat(state['history_start']) if state.get('history_start') else today-timedelta(days=int(os.getenv('WB_HISTORY_DAYS','90')))
        pending=state.get('pending_cursor')
        # After downtime, cover the missed interval too; never jump straight to 7 days ago.
        recent=(today-timedelta(days=7)).isoformat()
        previous=(date.fromisoformat(state['completed_at'][:10])-timedelta(days=2)).isoformat() if state.get('completed_at') else start.isoformat()
        cursor=pending or min(recent,previous)
        meta.update(period_start=str(start),period_end=str(today-timedelta(days=1)),cursor=cursor)
        seen=set()
        for _ in range(120):
            page=await self.http.request(cabinet,source,'GET',f'https://statistics-api.wildberries.ru/api/v1/supplier/{source}',self.token(cabinet,'statistics'),interval=61,params={'dateFrom':cursor,'flag':0})
            meta['pages']+=1
            if not isinstance(page,list): raise SourceError('ERROR','Ожидался массив событий Statistics')
            if not page:
                self.store.cover(run,cabinet,source,max(start,today-timedelta(days=90)),today-timedelta(days=1))
                self.store.sync_state(cabinet,source,{'history_start':str(start),'completed_at':now(),'pending_cursor':None})
                return
            signature=key(page)
            if signature in seen: raise SourceError('PARTIAL','Повтор страницы: полная загрузка не подтверждена')
            seen.add(signature)
            records=[]
            for obj in page:
                n=nm_id(obj.get('nmId'))
                identity=obj.get('srid') if source=='orders' else obj.get('saleID')
                if n is None or not identity: raise SourceError('PARTIAL','Событие без nmId или уникального ID; нельзя безопасно учитывать')
                day=event_day(obj.get('date'))
                price=number(obj.get('priceWithDisc'))
                buyer=number(obj.get('finishedPrice'))
                model={'Склад WB':'FBO','Склад продавца':'FBS'}.get(obj.get('warehouseType'),'UNKNOWN')
                event_type='order' if source=='orders' else 'sale' if str(identity).startswith('S') else 'return' if str(identity).startswith('R') else 'unknown'
                if event_type=='unknown': raise SourceError('PARTIAL','Неизвестный тип saleID, продажи/возвраты не смешаны')
                rec={'event_key':key(cabinet,source,str(identity)),'nm_id':str(n),'day':day,
                     'model_basis':'warehouseType from Statistics; warehouse ownership, not an independently confirmed delivery method',
                     'units':1,'model':model,'barcode':str(obj.get('barcode') or ''),'size':str(obj.get('techSize') or ''),
                     'order_price':price if price and price>0 else None,'buyer_price':buyer if buyer and buyer>0 else None,
                     'is_cancel':bool(obj.get('isCancel')) if isinstance(obj.get('isCancel'),bool) else None,
                     'cancel_day':event_day(obj['cancelDate']) if obj.get('cancelDate') and str(obj['cancelDate'])[:4]!='0001' else None,
                     'event_type':event_type,'source_last_change':str(obj.get('lastChangeDate') or ''),
                     'product':{'supplier_article':obj.get('supplierArticle'),'brand':obj.get('brand'),'category':obj.get('subject'),'life_status':'OBSERVED_ACTIVITY'}}
                records.append(rec)
            self.store.events(run,cabinet,source,records)
            meta['records']+=len(records)
            raw_cursor=page[-1].get('lastChangeDate')
            if not raw_cursor or str(raw_cursor)<=str(cursor): raise SourceError('PARTIAL','Курсор не продвинулся; полный итог запрещён')
            cursor=str(raw_cursor)  # Preserve fractional seconds exactly.
            meta['cursor']=cursor
            self.store.sync_state(cabinet,source,{**state,'history_start':str(start),'pending_cursor':cursor})
        raise SourceError('PARTIAL','Достигнут предел страниц; продолжение при следующей синхронизации')

    async def stocks(self,run,cabinet,kind,meta):
        offset,seen=0,set()
        source='stocks_'+kind
        snapshot=meta['requested_at']
        while True:
            data=await self.http.request(cabinet,source,'POST','https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/'+('wb' if kind=='wb' else 'seller')+'-warehouses',self.token(cabinet,'analytics'),json={'limit':250000,'offset':offset})
            meta['pages']+=1
            if data is None: items=[]
            else:
                items=data.get('data',{}).get('items') if isinstance(data,dict) else None
                if not isinstance(items,list): raise SourceError('ERROR','Ожидался data.items остатков')
            batch=[]
            for obj in items:
                n=nm_id(obj.get('nmId')); q=count(obj.get('quantity'))
                if n is None or q is None or obj.get('chrtId') is None or obj.get('warehouseId') is None: raise SourceError('PARTIAL','Неизвестная строка остатков')
                identity=key(n,str(obj['chrtId']),str(obj['warehouseId']))
                if identity in seen: raise SourceError('PARTIAL','Повтор варианта/склада в остатках')
                seen.add(identity)
                batch.append({'observation_key':identity,'nm_id':str(n),'variant_id':str(obj['chrtId']),
                              'warehouse_id':str(obj['warehouseId']),'warehouse_name':obj.get('warehouseName'),
                              'model':'FBO' if kind=='wb' else 'FBS','quantity':q,'in_way_to_client':count(obj.get('inWayToClient')),
                              'in_way_from_client':count(obj.get('inWayFromClient')),'snapshot_at':snapshot,'status':'OK','run_id':run})
                batch[-1].update(snapshot_time_basis='REQUEST_OBSERVATION_TIME; upstream snapshot time not supplied',upstream_snapshot_at=None)
            self.store.observations(cabinet,source,batch,snapshot)
            meta['records']+=len(batch); meta['snapshot_at']=snapshot
            if len(items)<250000: return
            offset+=len(items); meta['cursor']=str(offset)

    async def catalog(self,run,cabinet,meta):
        for life,path in [('ACTIVE','list'),('TRASH','trash')]:
            cursor={'limit':100}; seen=set()
            while True:
                data=await self.http.request(cabinet,'catalog','POST',f'https://content-api.wildberries.ru/content/v2/get/cards/{path}',self.token(cabinet,'content'),interval=1,json={'settings':{'sort':{'ascending':True},'cursor':cursor,'filter':{'withPhoto':-1}}})
                meta['pages']+=1
                cards=data.get('cards') if isinstance(data,dict) else None
                if not isinstance(cards,list): raise SourceError('ERROR','Ожидался массив cards')
                with self.store.connect() as db:
                    for obj in cards:
                        n=nm_id(obj.get('nmID'))
                        if n is None or n in seen: raise SourceError('PARTIAL','Повтор/ошибка nmID в каталоге')
                        seen.add(n)
                        attrs={'supplier_article':obj.get('vendorCode'),'title':obj.get('title'),'brand':obj.get('brand'),'category':obj.get('subjectName'),
                               'life_status':life,'created_at':obj.get('createdAt'),
                               'variants':[{'variant_id':str(x.get('chrtID') or ''),'size':str(x.get('techSize') or ''),'barcodes':[str(b) for b in x.get('skus',[])]} for x in obj.get('sizes',[])]}
                        old=db.execute('SELECT payload FROM v3_products WHERE cabinet_id=? AND nm_id=?',(cabinet,str(n))).fetchone()
                        before=json.loads(old[0]) if old else None
                        self.store.product(db,cabinet,str(n),attrs,meta['requested_at'])
                        attrs['content_hash']=key(obj.get('description'),obj.get('photos'),obj.get('characteristics'),obj.get('title'))
                        monitored={k:attrs[k] for k in ('supplier_article','title','brand','category','life_status','content_hash')}
                        self.store.product(db,cabinet,str(n),{'content_hash':attrs['content_hash']},meta['requested_at'])
                        if before and any(before.get(k)!=v for k,v in monitored.items()):
                            event={'observation_key':key(n,meta['requested_at']),'nm_id':str(n),'observed_at':meta['requested_at'],'event_type':'CARD_FIELDS_CHANGED','fields':dumps(monitored),'status':'OK','run_id':run}
                            event['before_fields']=dumps({k:before.get(k) for k in monitored})
                            db.execute('INSERT OR REPLACE INTO v3_observations VALUES(?,?,?,?,?)',(cabinet,'card_events',event['observation_key'],meta['requested_at'],dumps(event)))
                meta['records']+=len(cards)
                if len(cards)<100: break
                next_cursor=data.get('cursor',{})
                stamp=(next_cursor.get('updatedAt') or next_cursor.get('trashedAt'),next_cursor.get('nmID'))
                if not all(stamp) or dumps(cursor)==dumps(next_cursor): raise SourceError('PARTIAL','Курсор каталога не продвинулся')
                cursor={k:v for k,v in next_cursor.items() if k in {'updatedAt','trashedAt','nmID'}};cursor['limit']=100

    async def prices(self,run,cabinet,meta):
        offset,seen=0,set()
        while True:
            data=await self.http.request(cabinet,'prices','GET','https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter',self.token(cabinet,'prices'),interval=1,params={'limit':1000,'offset':offset})
            meta['pages']+=1
            items=data.get('data',{}).get('listGoods') if isinstance(data,dict) else None
            if not isinstance(items,list): raise SourceError('ERROR','Ожидался data.listGoods')
            batch=[]
            for obj in items:
                n=nm_id(obj.get('nmID'))
                if n is None or n in seen: raise SourceError('PARTIAL','Повтор/ошибка ID цены')
                seen.add(n)
                for size in obj.get('sizes',[]):
                    sid=str(size.get('sizeID') or '')
                    if not sid: raise SourceError('PARTIAL','Цена варианта без sizeID')
                    for price_type,field in [('SELLER_LIST','price'),('SELLER_DISCOUNTED','discountedPrice'),('WB_CLUB_OFFER','clubDiscountedPrice')]:
                        value=number(size.get(field))
                        batch.append({'observation_key':key(n,sid,price_type),'nm_id':str(n),'size_id':sid,'price_type':price_type,'value':value,
                                      'seller_discount':number(obj.get('discount'))/100 if number(obj.get('discount')) is not None else None,
                                      'currency':obj.get('currencyIsoCode4217'),'snapshot_at':meta['requested_at'],'status':'OK' if value is not None else 'MISSING','run_id':run,
                                      'product':{'supplier_article':obj.get('vendorCode')}})
            self.store.observations(cabinet,'prices',batch,meta['requested_at']);meta['records']+=len(batch);meta['snapshot_at']=meta['requested_at']
            if len(items)<1000:return
            offset+=len(items);meta['cursor']=str(offset)

    async def funnel(self,run,cabinet,meta):
        today=datetime.now(MSK).date();start=today-timedelta(days=6);end=today-timedelta(days=1)
        ids=[int(x['nm_id']) for x in self.store.rows('SELECT nm_id FROM v3_products WHERE cabinet_id=?',(cabinet,))]
        ids.sort()
        if not ids:raise SourceError('MISSING','Нет известных nmId; права воронки пока не проверены')
        meta.update(period_start=str(start),period_end=str(end))
        state=self.store.sync_state(cabinet,'funnel')
        ids_hash=key(ids)
        offset=state.get('offset',0) if state.get('end')==str(end) and state.get('ids_hash')==ids_hash else 0
        for begin in range(offset,len(ids),20):
            chunk=ids[begin:begin+20]
            data=await self.http.request(cabinet,'funnel','POST','https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history',self.token(cabinet,'analytics'),json={'nmIds':chunk,'selectedPeriod':{'start':str(start),'end':str(end)},'aggregationLevel':'day','skipDeletedNm':False})
            meta['pages']+=1
            if not isinstance(data,list): raise SourceError('ERROR','Ожидался массив дневной воронки')
            seen=set()
            with self.store.connect() as db:
                for obj in data:
                    n=nm_id(obj.get('product',{}).get('nmId'))
                    if n not in chunk or n in seen:raise SourceError('PARTIAL','Повтор/чужой nmId в воронке')
                    seen.add(n)
                    for daily in obj.get('history',[]):
                        day=event_day(daily.get('date'))
                        if not str(start)<=day<=str(end):raise SourceError('PARTIAL','Воронка вне запрошенного периода')
                        values={k:count(daily.get(field)) for k,field in [('views','openCount'),('carts','cartCount'),('funnel_orders','orderCount'),('wishlist','addToWishlistCount'),('funnel_buyout_units','buyoutCount')]}
                        values['status']='OK' if all(v is not None for v in values.values()) else 'PARTIAL'
                        db.execute('INSERT OR REPLACE INTO v3_funnel VALUES(?,?,?,?,?,?)',(cabinet,str(n),day,dumps(values),run,now()));meta['records']+=1
            meta['cursor']=str(begin+len(chunk))
            self.store.sync_state(cabinet,'funnel',{'end':str(end),'offset':begin+len(chunk),'ids_hash':ids_hash})
        self.store.sync_state(cabinet,'funnel',{'end':str(end),'offset':0,'ids_hash':ids_hash})

    async def funnel_period(self,run,cabinet,meta):
        from analytics_client import products,metric
        today=datetime.now(MSK).date();last_end=today-timedelta(days=2);last_start=last_end-timedelta(days=6)
        first_end=last_start-timedelta(days=1);first_start=first_end-timedelta(days=6)
        ids=[int(x['nm_id']) for x in self.store.rows('SELECT nm_id FROM v3_products WHERE cabinet_id=?',(cabinet,))]
        ids.sort()
        if not ids:raise SourceError('MISSING','Нет известных nmId; права агрегированной воронки пока не проверены')
        state=self.store.sync_state(cabinet,'funnel_period');ids_hash=key(ids)
        resume=state.get('offset',0) if state.get('end')==str(last_end) and state.get('ids_hash')==ids_hash else 0
        for begin in range(resume,len(ids),1000):
            chunk=ids[begin:begin+1000];offset=0;seen=set()
            while True:
                payload={'selectedPeriod':{'start':str(last_start),'end':str(last_end)},'pastPeriod':{'start':str(first_start),'end':str(first_end)},'nmIds':chunk,'skipDeletedNm':False,'limit':1000,'offset':offset}
                data=await self.http.request(cabinet,'funnel_period','POST','https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products',self.token(cabinet,'analytics'),json=payload)
                page=products(data);meta['pages']+=1;batch=[];cards=[]
                for item in page:
                    n=nm_id(item.get('product',{}).get('nmId'))
                    if n not in chunk or n in seen:raise SourceError('PARTIAL','Повтор/чужой nmId в агрегированной воронке')
                    seen.add(n)
                    for name,start,end in [('past',first_start,first_end),('selected',last_start,last_end)]:
                        obj=item.get('statistic',{}).get(name);m=metric(obj,start,end)
                        if m is None:continue
                        values={'observation_key':key(n,start,end),'nm_id':str(n),'period_start':str(start),'period_end':str(end),'views':m.views,'carts':m.carts,'funnel_orders':m.orders,'wishlist':m.wishlist,'run_id':run,'status':'OK' if all(v is not None for v in (m.views,m.carts,m.orders,m.wishlist)) else 'PARTIAL'}
                        delivery=obj.get('timeToReady') or {}
                        values.update(delivery_days=count(delivery.get('days')),delivery_hours=count(delivery.get('hours')),delivery_mins=count(delivery.get('mins')),delivery_scope='WB period average, not a regional delivery promise')
                        values['delivery_status']='OK' if all(values[k] is not None for k in ('delivery_days','delivery_hours','delivery_mins')) else 'MISSING'
                        batch.append(values)
                    product=item.get('product',{})
                    cards.append({'observation_key':str(n),'nm_id':str(n),'product_rating':number(product.get('productRating')),'feedback_rating':number(product.get('feedbackRating')),'run_id':run,'status':'OK','time_semantics':'Current product metadata observed at request time, not historical daily rating'})
                    cards[-1].update(product_rating_status='OK' if cards[-1]['product_rating'] is not None else 'MISSING',feedback_rating_status='OK' if cards[-1]['feedback_rating'] is not None else 'MISSING')
                self.store.observations(cabinet,'funnel_period',batch,meta['requested_at'])
                self.store.observations(cabinet,'card_metrics',cards,meta['requested_at']);meta['records']+=len(batch)
                if len(page)<1000:break
                offset+=len(page)
            self.store.sync_state(cabinet,'funnel_period',{'end':str(last_end),'offset':begin+len(chunk),'ids_hash':ids_hash})
        self.store.sync_state(cabinet,'funnel_period',{'end':str(last_end),'offset':0,'ids_hash':ids_hash})
        meta.update(period_start=str(first_start),period_end=str(last_end))

    async def search(self,run,cabinet,meta):
        from analytics_client import analytics_client
        today=datetime.now(MSK).date();end=today-timedelta(days=2);start=end-timedelta(days=6)
        ids=[int(x['nm_id']) for x in self.store.rows('SELECT nm_id FROM v3_products WHERE cabinet_id=?',(cabinet,))]
        if not ids:raise SourceError('MISSING','Нет известных nmId; права поиска пока не проверены')
        # One current-period statistic per interval; never manufactured daily positions.
        values=await analytics_client._search_period(cabinet,start,end,ids)
        batch=[{'observation_key':key(n,start,end),'nm_id':str(n),'period_start':str(start),'period_end':str(end),'query_scope':'ALL_SEARCH_AND_SUBSTITUTED','avg_position':p,'status':'OK','run_id':run} for n,p in values.items()]
        self.store.observations(cabinet,'search',batch,meta['requested_at']);meta.update(records=len(batch),period_start=str(start),period_end=str(end),pages=(len(ids)+49)//50)

    async def ads(self,run,cabinet,meta):
        token=self.token(cabinet,'ads')
        listing=await self.http.request(cabinet,'ads_list','GET','https://advert-api.wildberries.ru/adv/v1/promotion/count',token,interval=1)
        if not isinstance(listing,dict) or not isinstance(listing.get('adverts'),list):raise SourceError('ERROR','Ожидался массив adverts')
        campaigns=[];ids=[]
        for group in listing['adverts']:
            for advert in group.get('advert_list',[]):
                campaign=str(advert.get('advertId') or '')
                if not campaign:raise SourceError('PARTIAL','Кампания без advertId')
                campaigns.append({'observation_key':key(campaign),'campaign_id':campaign,'campaign_status':group.get('status'),'campaign_type':group.get('type'),
                                  'source_change_time':advert.get('changeTime'),'observed_at':meta['requested_at'],'status':'OK','run_id':run})
                if group.get('status') in {7,9,11}:ids.append(campaign)
        self.store.observations(cabinet,'campaigns',campaigns,meta['requested_at'])
        today=datetime.now(MSK).date();end=today-timedelta(days=1)
        state=self.store.sync_state(cabinet,'ads')
        full_history=state.get('history_complete',False)
        windows=[(end-timedelta(days=6),end)] if full_history else [(end-timedelta(days=30*i+29),end-timedelta(days=30*i)) for i in range(3)]
        start=min(x[0] for x in windows)
        meta.update(period_start=str(start),period_end=str(end),pages=1)
        plan=[(ids[offset:offset+50],lo,hi) for lo,hi in windows for offset in range(0,len(ids),50)]
        plan_hash=key(plan)
        cursor=state.get('offset',0) if state.get('job_day')==str(end) and state.get('plan_hash')==plan_hash else 0
        for job_index,(chunk,window_start,window_end) in enumerate(plan):
            if job_index<cursor:continue
            data=await self.http.request(cabinet,'ads','GET','https://advert-api.wildberries.ru/adv/v3/fullstats',token,params={'ids':','.join(chunk),'beginDate':str(window_start),'endDate':str(window_end)})
            meta['pages']+=1
            if not isinstance(data,list):raise SourceError('ERROR','Ожидался массив статистики кампаний')
            batch=[];seen=set()
            for campaign in data:
                cid=str(campaign.get('advertId'))
                if cid not in chunk:raise SourceError('PARTIAL','Чужой advertId в статистике')
                for daily in campaign.get('days',[]):
                    day=str(daily.get('date') or '')[:10]
                    if not str(window_start)<=day<=str(window_end):raise SourceError('PARTIAL','Рекламная дата вне периода')
                    daily_spend=number(daily.get('sum'));subtotal=0;full=True
                    for app in daily.get('apps',[]):
                        app_id=str(app.get('appType'))
                        for nm in app.get('nms',[]):
                            n=nm_id(nm.get('nmId'))
                            if n is None:raise SourceError('PARTIAL','Реклама без nmId')
                            identity=key(cid,n,day,app_id)
                            if identity in seen:raise SourceError('PARTIAL','Повтор кампании/nmId/дня/платформы')
                            seen.add(identity)
                            spend=number(nm.get('sum'));subtotal+=spend or 0;full=full and spend is not None
                            values={'observation_key':identity,'nm_id':str(n),'campaign_id':cid,'app_type':app_id,'day':day,
                                    'spend':spend,'impressions':count(nm.get('views')),'clicks':count(nm.get('clicks')),
                                    'attributed_orders':count(nm.get('orders')),'attributed_units':count(nm.get('shks')),
                                    'attributed_amount':number(nm.get('sum_price')),'attribution_window':'WB source attribution; exact window unverified',
                                    'attribution_status':'PARTIAL','currency':campaign.get('currency'),'source':'GET /adv/v3/fullstats.days.apps.nms',
                                    'status':'OK' if spend is not None else 'PARTIAL','run_id':run,'snapshot_at':meta['requested_at']}
                            batch.append(values)
                    if daily_spend is None or not full or abs(daily_spend-subtotal)>.05:
                        raise SourceError('PARTIAL','Расход по SKU/платформам не свёлся с расходом кампании; нельзя объявить полный расход')
            self.store.observations(cabinet,'ads',batch,meta['requested_at']);meta['records']+=len(batch)
            self.store.sync_state(cabinet,'ads',{'job_day':str(end),'offset':job_index+1,'history_complete':full_history,'plan_hash':plan_hash})
        self.store.sync_state(cabinet,'ads',{'job_day':str(end),'offset':0,'history_complete':True})

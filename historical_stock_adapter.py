"""Resumable daily CSV jobs, max one new job/model/day. Wide CSV is parsed by date."""
import csv
from datetime import date,timedelta
import io
import uuid
import zipfile
import aiohttp
from config import CABINETS,HTTP_TIMEOUT_SECONDS
from data_quality import nm_id,count
from stock_history_client import StockHistoryClient,StockHistoryError
from history_store import key,now


def parse_details(archive,start,end,model,run_id):
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        files=[n for n in z.namelist() if n.lower().endswith('.csv')]
        if not files:raise StockHistoryError('Нет CSV в ZIP истории остатков')
        for name in files:
            raw=z.read(name)
            try:text=raw.decode('utf-8-sig')
            except UnicodeDecodeError:text=raw.decode('cp1251')
            try:dialect=csv.Sniffer().sniff(text[:4096],delimiters=',;\t')
            except csv.Error:dialect=csv.excel
            reader=csv.DictReader(io.StringIO(text),dialect=dialect)
            fields={StockHistoryClient._norm(h):h for h in reader.fieldnames or []}
            nm=fields.get('nmid');variant=fields.get('chrtid');office=fields.get('officename')
            day_columns=[]
            d=start
            while d<=end:
                field=fields.get(StockHistoryClient._norm(d.strftime('%d.%m.%Y'))) or fields.get(StockHistoryClient._norm(d.isoformat()))
                if field:day_columns.append((d,field))
                d+=timedelta(days=1)
            if not nm or not variant or not office or not day_columns:raise StockHistoryError(f'Неизвестные колонки исторических остатков: {reader.fieldnames}')
            seen=set()
            for row in reader:
                n=nm_id(row[nm]);v=str(row[variant] or '').strip();warehouse=str(row[office] or '').strip()
                if not n or not v or not warehouse:raise StockHistoryError('Исторический остаток без nmID/ChrtID/OfficeName')
                identity=key(n,v,warehouse)
                if identity in seen:raise StockHistoryError('Повтор ключа nm/variant/office в исторических остатках')
                seen.add(identity)
                for d,field in day_columns:
                    qty=count(row[field])
                    if qty is None:continue  # Blank is unavailable, never zero.
                    yield {'observation_key':identity,'nm_id':str(n),'variant_id':v,'warehouse_id':None,'warehouse_name':warehouse,
                           'model':model,'quantity':qty,'snapshot_at':str(d)+'T23:59:00+03:00','observed_at':now(),'status':'OK','run_id':run_id,
                           'scope_key':'OfficeName + ChrtID; warehouse_id not supplied by CSV','source_kind':'HISTORICAL_2359'}


async def collect(store,cabinet,kind,run,end,meta):
    source='stock_history_'+kind
    state=store.sync_state(cabinet,source)
    if state.get('completed_day')==str(end):
        meta.update(cache_hit=True,records=state.get('records',0),period_start=state.get('start'),period_end=str(end),last_success_at=state.get('completed_at'))
        return
    if state.get('job_day')!=str(end):
        state={'job_day':str(end),'id':str(uuid.uuid4()),'start':str(end-timedelta(days=6 if state.get('completed_day') else 89)),'created':False}
        store.sync_state(cabinet,source,state)
    client=StockHistoryClient();cab=CABINETS[cabinet];token=cab.analytics_token or cab.token
    if not token:raise StockHistoryError('Не задан токен Аналитики')
    ids=[int(r['nm_id']) for r in store.rows('SELECT nm_id FROM v3_products WHERE cabinet_id=?',(cabinet,))]
    if not ids:
        from collector import SourceError
        raise SourceError('MISSING','Нет известных nmId; права исторических остатков ещё не проверены')
    start=date.fromisoformat(state['start'])
    payload={'id':state['id'],'reportType':'STOCK_HISTORY_DAILY_CSV','userReportName':f'History {kind} {end}',
             'params':{'currentPeriod':{'start':str(start),'end':str(end)},'stockType':kind,'skipDeletedNm':False,'nmIds':ids}}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),headers={'Authorization':token}) as session:
        if not state['created']:
            await client._create(session,payload)
            state['created']=True;store.sync_state(cabinet,source,state)
        await client._wait_ready(session,state['id'])
        archive=await client._download(session,state['id'])
    batch=[]
    meta.update(period_start=str(start),period_end=str(end),cursor=state['id'],pages=1)
    for record in parse_details(archive,start,end,'FBO' if kind=='wb' else 'FBS',run):
        batch.append(record)
        if len(batch)>=2000:
            store.observations(cabinet,source,batch,None);meta['records']+=len(batch);batch=[]
    if batch:store.observations(cabinet,source,batch,None);meta['records']+=len(batch)
    state.update(completed_day=str(end),completed_at=now(),records=meta['records']);store.sync_state(cabinet,source,state)

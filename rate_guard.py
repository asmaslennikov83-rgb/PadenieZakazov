"""Durable per-cabinet request pacing; no tokens in state or messages."""
import asyncio
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import math
import os
import time
from history_store import dumps


class Deferred(RuntimeError):
    def __init__(self,until):
        self.retry_at=datetime.fromtimestamp(until,timezone.utc).isoformat()
        local=datetime.fromtimestamp(until,timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Europe/Moscow'))
        super().__init__('Соблюдается пауза API. Следующая попытка не раньше '+local.strftime('%d.%m %H:%M:%S МСК')+'. Перезапуск не сокращает паузу.')


def retry_seconds(headers,fallback=65,epoch=None):
    epoch=time.time() if epoch is None else epoch
    delays=[]
    for name in ('X-Ratelimit-Retry','Retry-After','X-Ratelimit-Reset'):
        raw=headers.get(name)
        if raw is None:continue
        try:value=float(raw)
        except (ValueError,TypeError):
            try:value=parsedate_to_datetime(raw).timestamp()-epoch
            except (ValueError,TypeError,OverflowError):continue
        if math.isfinite(value) and value>=0:delays.append(value)
    return max(delays or [fallback])+1


class RateGuard:
    def __init__(self,store,clock=time.time):self.store,self.clock=store,clock

    def group(self,url):
        if '/sales-funnel/' in url:return 'sales-funnel'
        if '/stocks-report/' in url:return 'current-stocks'
        if '/downloads' in url:return 'analytics-downloads'
        return url

    def interval(self,cabinet,url,minimum):
        if '/api/v1/supplier/' in url:
            suffix={'spb_tt':'SPB_TT','spb_tt_group':'SPB_TT_GROUP'}[cabinet]
            # This distribution is configured for the user's confirmed personal tokens.
            kind=os.getenv('WB_STATISTICS_TOKEN_TYPE_'+suffix,'personal').lower().strip()
            if kind not in {'personal','service','base_with_secret'}:
                return max(minimum,10801 if url.endswith('/orders') else 7201)
        return minimum

    def reserve(self,cabinet,url,interval):
        group=self.group(url);epoch=self.clock()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            import json
            old=db.execute('SELECT payload FROM v3_sync WHERE cabinet_id=? AND source=?',('rate:'+cabinet,group)).fetchone()
            due=json.loads(old[0]).get('next_allowed_epoch',0) if old else 0
            if due>epoch:return due-epoch
            db.execute('INSERT OR REPLACE INTO v3_sync VALUES(?,?,?)',('rate:'+cabinet,group,dumps({'next_allowed_epoch':epoch+interval})))
        return 0

    async def before(self,cabinet,url,minimum):
        interval=self.interval(cabinet,url,minimum)
        while True:
            wait=self.reserve(cabinet,url,interval)
            if wait<=0:return
            if wait>65:raise Deferred(self.clock()+wait)
            await asyncio.sleep(wait)

    def limited(self,cabinet,url,headers,fallback=65):
        delay=retry_seconds(headers,max(fallback,self.interval(cabinet,url,0)),self.clock())
        group=self.group(url);until=self.clock()+delay
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            import json
            row=db.execute('SELECT payload FROM v3_sync WHERE cabinet_id=? AND source=?',('rate:'+cabinet,group)).fetchone()
            until=max(until,json.loads(row[0]).get('next_allowed_epoch',0) if row else 0)
            db.execute('INSERT OR REPLACE INTO v3_sync VALUES(?,?,?)',('rate:'+cabinet,group,dumps({'next_allowed_epoch':until,'reason':'HTTP 429'})))
        return Deferred(until)

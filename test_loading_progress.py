import asyncio
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

import test_modern as fixtures
seed=fixtures.seed
DAY=fixtures.DAY
from business_report import build,telegram,latest_sources
from collector import Collector,MSK,SourceError
from history_store import now


# New test classes inherit fixtures only, not the existing test methods.
import unittest
class LoadingTests(unittest.TestCase):
    setUp=fixtures.ModernTests.setUp
    tearDown=fixtures.ModernTests.tearDown

    def test_first_load_is_partial_and_explained(self):
        seed(self.store)
        with self.store.connect() as db:db.execute('DELETE FROM v3_coverage')
        self.store.source('loading','spb_tt','orders',status='PARTIAL',phase='RUNNING',last_success_at=None,pages=1,records=179,reason='Loading')
        r=build(self.store,DAY,['spb_tt'],save=False)
        self.assertIsNone(r['summary'][0]['ordered_units'])
        self.assertIn('Идёт первая загрузка',telegram(r,{}))
        self.assertIn('1 стр., 179 строк',telegram(r,{}))

    def test_refresh_retains_previously_confirmed_snapshot(self):
        seed(self.store)
        self.store.source('refresh','spb_tt','orders',status='PARTIAL',phase='RUNNING',last_success_at=now(),pages=0,records=0,reason='Loading')
        r=build(self.store,DAY,['spb_tt'],save=False)
        self.assertEqual(r['summary'][0]['status'],'OK')
        self.assertEqual(r['summary'][0]['ordered_units'],1)
        self.assertIn('ранее подтверждённый срез',telegram(r,{}))

    def test_failed_refresh_still_marks_snapshot_stale(self):
        seed(self.store)
        self.store.source('failed','spb_tt','orders',status='ERROR',phase='FAILED',last_success_at=now(),reason='HTTP 429')
        r=build(self.store,DAY,['spb_tt'],save=False)
        self.assertEqual(r['summary'][0]['status'],'PARTIAL')
        self.assertIsNone(r['summary'][0]['ordered_units'])

    def test_recovery_does_not_reorder_old_failed_run_after_latest_success(self):
        self.store.source('old','spb_tt','orders',status='PARTIAL',reason='Синхронизация выполняется',completed_at=None)
        self.store.source('new','spb_tt','orders',status='OK',reason='Done',completed_at=now())
        self.assertEqual(self.store.recover_interrupted_sources(),1)
        self.assertEqual(latest_sources(self.store,['spb_tt'])['spb_tt','orders']['run_id'],'new')
        self.assertEqual(self.store.sources('old')[0]['phase'],'INTERRUPTED')

    def test_startup_releases_old_sync_lease_without_touching_other_leases(self):
        self.store.lease('sync:spb_tt',1800)
        self.store.lease('another-purpose',1800)
        self.store.recover_interrupted_sources()
        self.assertTrue(self.store.lease('sync:spb_tt',1800))
        self.assertIsNone(self.store.lease('another-purpose',1800))

    def test_version_change_creates_separate_immutable_report_revision(self):
        seed(self.store)
        first=build(self.store,DAY,['spb_tt'])
        with patch('business_report.VERSION','3.0.2-test'):
            second=build(self.store,DAY,['spb_tt'])
        self.assertEqual(second['revision'],first['revision']+1)


class AsyncLoadingTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.AsyncModernTests.setUp
    tearDown=fixtures.AsyncModernTests.tearDown

    async def test_saved_progress_visible_before_terminal_empty_page(self):
        today=datetime.now(MSK).date()
        obj={'srid':'1','nmId':123,'date':str(today-timedelta(days=1))+'T12:00:00','lastChangeDate':str(today)+'T10:00:00.12345','priceWithDisc':100,'isCancel':False}
        calls=0
        async def request(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:return [obj]
            live=self.store.sources('r')[0]
            self.assertEqual(live['pages'],1);self.assertEqual(live['records'],1)
            self.assertEqual(live['phase'],'RUNNING');self.assertIsNone(live['completed_at'])
            self.assertEqual(self.store.rows('SELECT * FROM v3_coverage'),[])
            return []
        c=Collector(self.store,SimpleNamespace(request=request))
        result=await c.sync_source('r','spb_tt','orders',lambda m:c.ledger('r','spb_tt','orders',m))
        self.assertEqual(result['phase'],'FINISHED');self.assertEqual(result['pages'],2)
        self.assertTrue(self.store.rows('SELECT * FROM v3_coverage'))

    async def test_all_optional_sources_visible_in_queue_before_orders_finish(self):
        c=Collector(self.store,SimpleNamespace(request=AsyncMock()))
        async def ledger(run,cabinet,source,meta):
            if source=='orders':
                rows={r['source']:r for r in self.store.sources(run)}
                self.assertEqual(rows['catalog']['phase'],'QUEUED')
                self.assertEqual(rows['funnel']['phase'],'QUEUED')
                self.assertIn('ещё не проверены',rows['funnel']['reason'])
        c.ledger=ledger
        for name in ('catalog','prices','stocks','funnel_period','funnel','ads'):setattr(c,name,AsyncMock())
        with patch.dict('os.environ',{'WB_ENABLE_SEARCH':'false','WB_ENABLE_STOCK_HISTORY':'false'}):
            await c.sync(['spb_tt'],extras=True)
        self.assertTrue(all(s['phase']=='FINISHED' for s in self.store.sources()))

    async def test_report_cache_invalidated_when_source_finishes(self):
        import modern_ui
        seed(self.store)
        settings=SimpleNamespace(cabinet_mode='spb_tt',threshold=.15)
        modern_ui.cache.clear()
        with patch.object(modern_ui,'store',self.store),patch.object(modern_ui,'get_user_settings',AsyncMock(return_value=settings)),patch.object(modern_ui,'build',side_effect=[{'value':1},{'value':2}]) as builder:
            first=await modern_ui.report_for(42)
            self.assertEqual(first['value'],1)
            self.assertEqual((await modern_ui.report_for(42))['value'],1)
            self.store.source('new','spb_tt','orders',status='OK',completed_at=now(),pages=2,records=5)
            self.assertEqual((await modern_ui.report_for(42))['value'],2)
            self.assertEqual(builder.call_count,2)
        modern_ui.cache.clear()

    async def test_manual_refresh_does_not_claim_completion_for_active_background_job(self):
        import modern_ui
        self.store.lease('sync:spb_tt',1800)
        event=SimpleNamespace(answer=AsyncMock())
        with patch.object(modern_ui,'store',self.store),patch.object(modern_ui,'get_user_settings',AsyncMock(return_value=SimpleNamespace(cabinet_mode='spb_tt'))):
            await modern_ui.request_refresh(event,SimpleNamespace(),42,42)
        self.assertIn('Уже идёт',event.answer.call_args.args[0])
        self.assertEqual(len(modern_ui.jobs),0)

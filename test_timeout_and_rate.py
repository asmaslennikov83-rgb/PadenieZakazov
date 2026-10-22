import asyncio
from datetime import datetime,timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch
import test_modern as fixtures
from collector import Collector
from rate_guard import RateGuard,Deferred,retry_seconds


class GuardTests(unittest.TestCase):
    setUp=fixtures.ModernTests.setUp
    tearDown=fixtures.ModernTests.tearDown
    url='https://statistics-api.wildberries.ru/api/v1/supplier/orders'

    def test_unknown_token_uses_conservative_statistics_interval(self):
        guard=RateGuard(self.store)
        with patch.dict('os.environ',{'WB_STATISTICS_TOKEN_TYPE_SPB_TT':'unknown'}):self.assertEqual(guard.interval('spb_tt',self.url,61),10801)
        with patch.dict('os.environ',{'WB_STATISTICS_TOKEN_TYPE_SPB_TT':'personal'}):self.assertEqual(guard.interval('spb_tt',self.url,61),61)

    def test_reservation_survives_new_client_and_is_separate_per_cabinet(self):
        first=RateGuard(self.store,clock=lambda:1000)
        self.assertEqual(first.reserve('spb_tt',self.url,61),0)
        second=RateGuard(self.store,clock=lambda:1001)
        self.assertEqual(second.reserve('spb_tt',self.url,61),60)
        self.assertEqual(second.reserve('spb_tt_group',self.url,61),0)

    def test_429_uses_longest_header_and_persists_until_restart(self):
        guard=RateGuard(self.store,clock=lambda:1000)
        with patch.dict('os.environ',{'WB_STATISTICS_TOKEN_TYPE_SPB_TT':'personal'}):
            exc=guard.limited('spb_tt',self.url,{'X-Ratelimit-Retry':'100','Retry-After':'200','X-Ratelimit-Reset':'500'})
        self.assertIsInstance(exc,Deferred)
        self.assertEqual(RateGuard(self.store,clock=lambda:1100).reserve('spb_tt',self.url,61),401)

    def test_invalid_headers_cannot_inject_nonfinite_wait(self):
        self.assertEqual(retry_seconds({'Retry-After':'NaN','X-Ratelimit-Retry':'broken'},65,1000),66)

    def test_http_date_retry_after(self):
        self.assertEqual(retry_seconds({'Retry-After':'Thu, 01 Jan 1970 00:20:00 GMT'},65,1000),201)

    def test_funnel_period_and_daily_share_limit_bucket(self):
        guard=RateGuard(self.store,clock=lambda:1000)
        guard.reserve('spb_tt','https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products',21)
        self.assertEqual(guard.reserve('spb_tt','https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history',21),21)

    def test_brief_uses_sql_totals_without_building_full_report(self):
        from brief_report import brief
        fixtures.seed(self.store,base=2,fact=1)
        with patch('business_report.build',side_effect=AssertionError('full export must not run')):
            text=brief(self.store,fixtures.DAY,['spb_tt'])
        self.assertIn('Покрытие 15/15',text);self.assertIn('База/день: 2',text)
        self.assertIn('-50.0%',text);self.assertIn('100 ₽',text)

    def test_brief_never_declares_partial_counts_as_full(self):
        from brief_report import brief
        fixtures.seed(self.store,base=2,fact=1)
        with self.store.connect() as db:db.execute('DELETE FROM v3_coverage')
        text=brief(self.store,fixtures.DAY,['spb_tt'])
        self.assertIn('Единицы: Н/Д; известный подытог 1',text)
        self.assertIn('Сумма заказов: Н/Д',text)

    def test_brief_real_zero_is_preserved(self):
        from brief_report import brief
        fixtures.seed(self.store,fact=0)
        text=brief(self.store,fixtures.DAY,['spb_tt'])
        self.assertIn('Единицы: 0;',text);self.assertIn('Сумма заказов: 0 ₽',text)


class AsyncSafetyTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.AsyncModernTests.setUp
    tearDown=fixtures.AsyncModernTests.tearDown

    async def test_long_wait_returns_visible_deferral_without_sleep(self):
        guard=RateGuard(self.store,clock=lambda:1000);url=GuardTests.url
        guard.reserve('spb_tt',url,10801)
        with patch('rate_guard.asyncio.sleep',AsyncMock()) as sleep:
            with self.assertRaises(Deferred):await guard.before('spb_tt',url,61)
            sleep.assert_not_awaited()

    async def test_http_429_defers_after_one_request_and_saves_header_pause(self):
        from collector import ReadOnlyHTTP
        class Context:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
        response=Context();response.status=429;response.headers={'X-Ratelimit-Retry':'600'}
        session=Context();session.request=Mock(return_value=response)
        with patch('collector.aiohttp.ClientSession',return_value=session),patch.dict('os.environ',{'WB_STATISTICS_TOKEN_TYPE_SPB_TT':'personal'}):
            client=ReadOnlyHTTP(self.store)
            with self.assertRaises(Deferred):await client.request('spb_tt','orders','GET',GuardTests.url,'test-only',interval=61)
        self.assertEqual(session.request.call_count,1)
        state=self.store.sync_state('rate:spb_tt',GuardTests.url)
        self.assertEqual(state['reason'],'HTTP 429')

    async def test_deferral_has_retry_time_in_source_status(self):
        c=Collector(self.store)
        async def fail(meta):raise Deferred(9999999999)
        result=await c.sync_source('r','spb_tt','orders',fail)
        self.assertEqual(result['phase'],'WAITING_RETRY');self.assertTrue(result['retry_at'])
        self.assertEqual(self.store.rows('SELECT * FROM v3_coverage'),[])

    async def test_initial_load_is_15_days_then_history_expands_in_background(self):
        from collector import MSK
        http=SimpleNamespace(request=AsyncMock(return_value=[]));c=Collector(self.store,http)
        today=datetime.now(MSK).date()
        with patch.dict('os.environ',{'WB_INITIAL_DAYS':'15','WB_HISTORY_DAYS':'90'}):
            await c.sync_source('first','spb_tt','orders',lambda m:c.ledger('first','spb_tt','orders',m))
            self.assertEqual(http.request.call_args.kwargs['params']['dateFrom'],str(today-timedelta(days=15)))
            self.assertEqual(len(self.store.rows('SELECT * FROM v3_coverage')),15)
            await c.sync_source('second','spb_tt','orders',lambda m:c.ledger('second','spb_tt','orders',m))
            self.assertEqual(http.request.call_args.kwargs['params']['dateFrom'],str(today-timedelta(days=30)))
            self.assertEqual(len(self.store.rows('SELECT * FROM v3_coverage')),30)
            self.assertFalse(self.store.sync_state('spb_tt','orders')['backfill_complete'])

    async def test_existing_90_day_cursor_is_resumed_without_reset(self):
        from collector import MSK
        today=datetime.now(MSK).date();cursor=str(today)+'T10:00:00.1234567'
        self.store.sync_state('spb_tt','orders',{'history_start':str(today-timedelta(days=90)),'pending_cursor':cursor})
        http=SimpleNamespace(request=AsyncMock(return_value=[]));c=Collector(self.store,http)
        await c.sync_source('r','spb_tt','orders',lambda m:c.ledger('r','spb_tt','orders',m))
        self.assertEqual(http.request.call_args.kwargs['params']['dateFrom'],cursor)
        self.assertEqual(len(self.store.rows('SELECT * FROM v3_coverage')),90)

    async def test_check_button_does_not_fetch_wb_and_sends_saved_summary(self):
        import modern_ui
        bot=SimpleNamespace(send_message=AsyncMock())
        with patch.object(modern_ui,'quick_report_for',AsyncMock(return_value='Saved report')),patch.object(modern_ui,'stage'),patch.object(modern_ui.collector,'sync',AsyncMock()) as sync:
            await modern_ui.quick_check(bot,42,42)
            await modern_ui.check_jobs[42]
            self.assertTrue(any('Saved report' in c.args for c in bot.send_message.call_args_list))
            sync.assert_not_awaited()

    async def test_check_reports_local_timeout_instead_of_silent_hour(self):
        import modern_ui
        bot=SimpleNamespace(send_message=AsyncMock())
        async def blocked(user):await asyncio.Event().wait()
        with patch.object(modern_ui,'quick_report_for',blocked),patch.object(modern_ui,'stage'),patch.object(modern_ui.asyncio,'wait',AsyncMock(return_value=(set(),set()))):
            await modern_ui.quick_check(bot,43,43)
            await modern_ui.check_jobs[43]
            self.assertTrue(any('90 секунд' in str(c.args) for c in bot.send_message.call_args_list))

    async def test_duplicate_click_does_not_start_another_build(self):
        import modern_ui
        bot=SimpleNamespace(send_message=AsyncMock());ready=asyncio.Event()
        async def report(user):await ready.wait();return 'Done'
        with patch.object(modern_ui,'quick_report_for',report),patch.object(modern_ui,'stage'):
            await modern_ui.quick_check(bot,44,44);job=modern_ui.check_jobs[44]
            await modern_ui.quick_check(bot,44,44)
            self.assertIs(modern_ui.check_jobs[44],job)
            ready.set();await job

    async def test_settings_failure_gets_terminal_error_message(self):
        import modern_ui
        bot=SimpleNamespace(send_message=AsyncMock())
        with patch.object(modern_ui,'quick_report_for',AsyncMock(side_effect=RuntimeError('database locked'))),patch.object(modern_ui,'stage'):
            await modern_ui.quick_check(bot,45,45);await modern_ui.check_jobs[45]
        self.assertTrue(any('Код диагностики' in str(c.args) for c in bot.send_message.call_args_list))

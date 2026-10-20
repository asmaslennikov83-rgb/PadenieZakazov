"""Offline regression tests. No API credentials or network calls required."""
import asyncio
from dataclasses import replace
from datetime import date
from io import BytesIO
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

# Pure data tests can run without Telegram/HTTP dependencies installed.
if importlib.util.find_spec('aiohttp') is None:
    stub = types.ModuleType('aiohttp')
    stub.ClientError = type('ClientError', (Exception,), {})
    sys.modules['aiohttp'] = stub
if importlib.util.find_spec('dotenv') is None:
    stub = types.ModuleType('dotenv')
    stub.load_dotenv = lambda: None
    sys.modules['dotenv'] = stub

from analytics_client import AnalyticsClient, AnalyticsError, FunnelMetrics, SearchMetrics, metric
from stock_history_client import StockHistoryClient, StockHistoryError
from data_quality import nm_id, count, complete_sum, percent
from analysis_service import build_cabinet_report
from config import CABINETS
from reporting import build_excel
import openpyxl


def archive(texts):
    out = BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for i, text in enumerate(texts):
            z.writestr(f'part{i}.csv', text.encode('utf-8-sig'))
    return out.getvalue()


class QualityTests(unittest.TestCase):
    def test_ids_are_exact_positive_integers(self):
        self.assertEqual(nm_id('123'), 123)
        self.assertEqual(nm_id('123.0'), 123)
        for value in ('123.5', 0, None, 'NaN', True):
            self.assertIsNone(nm_id(value))

    def test_missing_is_not_zero(self):
        self.assertIsNone(count(None))
        self.assertEqual(count(0), 0)
        self.assertIsNone(count('1.3'))
        self.assertIsNone(complete_sum([]))
        self.assertIsNone(complete_sum([0, None]))
        self.assertEqual(complete_sum([0, 0]), 0)
        self.assertIsNone(percent(0, 0))
        self.assertEqual(percent(0, 10), 0)

    def test_stock_parts_dates_sizes_and_explicit_zero(self):
        data = archive(['NmID;Date;StockQuantity\n123;19.10.2026;0\n124;19.10.2026;5\n124;18.10.2026;50\n',
                        'NmID;Date;StockQuantity\n124;2026-10-19;3\n'])
        self.assertEqual(StockHistoryClient()._parse_archive(data, date(2026,10,19)), {123:0,124:8})

    def test_unknown_stock_schema_fails_visibly(self):
        with self.assertRaises(StockHistoryError):
            StockHistoryClient()._parse_archive(archive(['NmID;Unexpected\n123;5']), date(2026,10,19))

    def test_official_wide_inventory_history_date_columns(self):
        data=archive(['VendorCode,Name,NmID,ChrtID,OfficeName,18.10.2026,19.10.2026\n'
                      'SKU,Product,123,1,Склад WB,100,0\n'
                      'SKU,Product,123,2,Склад WB,200,3\n'
                      'SKU2,Product,124,3,Склад WB,300,0\n'])
        self.assertEqual(StockHistoryClient()._parse_archive(data,date(2026,10,19)),{123:3,124:0})
        with self.assertRaises(StockHistoryError):
            StockHistoryClient()._parse_archive(data,date(2026,10,20))

    def test_empty_quantity_cannot_become_zero(self):
        with self.assertRaises(StockHistoryError):
            StockHistoryClient()._parse_archive(archive(['NmID;Quantity\n123;']), date(2026,10,19))

    def test_period_and_field_presence(self):
        start, end = date(2026,10,5), date(2026,10,11)
        obj = {'period':{'start':str(start),'end':str(end)},'openCount':0}
        result = metric(obj,start,end)
        self.assertEqual(result.views,0)
        self.assertIsNone(result.carts)
        with self.assertRaises(AnalyticsError):
            metric(obj,date(2026,10,6),end)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_period_api_reads_nested_schema_and_missing_ids(self):
        client = AnalyticsClient()
        first, last = date(2026,10,5), date(2026,10,12)
        def stat(start):
            return {'period':{'start':str(start),'end':str(date(2026,10,start.day+6))},
                    'openCount':20,'cartCount':4,'orderCount':1,'addToWishlist':0}
        client._post_json = AsyncMock(return_value={'data':{'products':[
            {'product':{'nmId':'123'},'statistic':{'past':stat(first),'selected':stat(last)}}]}})
        with patch.dict(CABINETS, {'spb_tt':replace(CABINETS['spb_tt'], token='fixture')}):
            a,b = await client.get_funnel_periods('spb_tt',first,date(2026,10,11),last,date(2026,10,18),[123,124])
        self.assertEqual(a[123].views,20)
        self.assertEqual(b[123].wishlist,0)
        self.assertNotIn(124,b)
        args = client._post_json.call_args.args
        self.assertEqual(args[1],'/api/analytics/v3/sales-funnel/products')
        self.assertEqual(args[2]['pastPeriod']['start'],'2026-10-05')

    async def test_duplicate_funnel_ids_fail(self):
        client = AnalyticsClient()
        obj = {'product':{'nmId':123}, 'statistic':{}}
        client._post_json = AsyncMock(return_value={'data':{'products':[obj,obj]}})
        with patch.dict(CABINETS, {'spb_tt':replace(CABINETS['spb_tt'], token='fixture')}):
            with self.assertRaises(AnalyticsError):
                await client.get_funnel_periods('spb_tt',date(2026,10,5),date(2026,10,11),date(2026,10,12),date(2026,10,18),[123])

    async def test_search_reads_current_separately_for_both_periods(self):
        client = AnalyticsClient()
        client._post_json = AsyncMock(side_effect=[
            {'data':{'products':[{'nmId':123,'avgPosition':{'current':15,'dynamics':200}}]}},
            {'data':{'products':[{'nmId':123,'avgPosition':{'current':5,'dynamics':100}}]}}])
        with patch.dict(CABINETS, {'spb_tt':replace(CABINETS['spb_tt'], token='fixture')}):
            got = await client.get_search_positions('spb_tt',date(2026,10,12),date(2026,10,18),date(2026,10,5),date(2026,10,11),[123])
        self.assertEqual(got[123],SearchMetrics(15,5))

    async def report(self, fbo, fbs, first=None, last=None, ids=(123,)):
        orders = [{'supplierArticle':'SKU','nmId':n,'date':'2026-10-06T12:00:00', 'priceWithDisc':100} for n in ids]
        stock = AsyncMock(side_effect=[fbo,fbs])
        with patch('analysis_service.wb_orders_client.get_period_orders', AsyncMock(return_value=orders)), \
             patch('analysis_service.analytics_client.get_funnel_periods', AsyncMock(return_value=(first or {},last or {}))), \
             patch('analysis_service.analytics_client.get_search_positions', AsyncMock(return_value={123:SearchMetrics(10,5)})), \
             patch('analysis_service.stock_history_client.get_stock_for_day', stock):
            return await build_cabinet_report('spb_tt','Fixture',15,'percent',date(2026,10,20))

    async def test_empty_stock_responses_remain_missing(self):
        row = (await self.report({},{})).rows[0]
        self.assertIsNone(row.fbo_stock_yesterday)
        self.assertIsNone(row.fbs_stock_yesterday)
        self.assertEqual(row.yesterday_orders,0)

    async def test_explicit_stock_zero_is_preserved(self):
        row = (await self.report({123:0},{123:0})).rows[0]
        self.assertEqual(row.fbo_stock_yesterday,0)
        self.assertEqual(row.fbs_stock_yesterday,0)

    async def test_second_stock_failure_preserves_first(self):
        report = await self.report({123:7},StockHistoryError('HTTP 403'))
        self.assertEqual(report.rows[0].fbo_stock_yesterday,7)
        self.assertIsNone(report.rows[0].fbs_stock_yesterday)
        self.assertTrue(any('403' in message for _,message in report.diagnostics))

    async def test_incomplete_nm_coverage_does_not_understate_totals(self):
        row = (await self.report({123:7},{123:0},ids=(123,124))).rows[0]
        self.assertIsNone(row.fbo_stock_yesterday)
        self.assertIsNone(row.search_position_last_7)

    async def test_funnel_uses_matching_complete_metrics_only(self):
        first={123:FunnelMetrics(100,10,2,None)}
        row=(await self.report({}, {},first,{})).rows[0]
        self.assertEqual(row.view_to_cart_first_7,10)
        self.assertEqual(row.cart_to_order_first_7,20)
        self.assertIsNone(row.views_last_7)
        self.assertIsNone(row.wishlist_first_7)

    async def test_excel_missing_total_diagnostics_and_ids(self):
        report=await self.report({123:7}, {})
        data=build_excel([report])
        w=openpyxl.load_workbook(BytesIO(data),data_only=True)
        s=w['Fixture']
        self.assertEqual(s['N2'].value,7)
        self.assertIsNone(s['O2'].value)
        self.assertIsNone(s['P2'].value)
        self.assertEqual(s['AI2'].value,'123')
        self.assertIn('FBS: НД',s['AJ2'].value)
        self.assertIn('Диагностика',w.sheetnames)
        self.assertEqual(s['D2'].value,0)
        self.assertIsNone(s['M2'].value)

    async def test_price_does_not_mix_buyer_and_seller_price(self):
        from analysis_service import _price
        self.assertIsNone(_price({'finishedPrice':75}))
        self.assertEqual(_price({'priceWithDisc':100,'finishedPrice':75}),100)

    async def test_renamed_article_does_not_duplicate_nm_analytics(self):
        orders=[{'supplierArticle':a,'nmId':123,'date':'2026-10-06T12:00:00','priceWithDisc':100}
                for a in ('Old name','New name')]
        with patch('analysis_service.wb_orders_client.get_period_orders',AsyncMock(return_value=orders)), \
             patch('analysis_service.analytics_client.get_funnel_periods',AsyncMock(return_value=({123:FunnelMetrics(100,10,2,0)},{}))), \
             patch('analysis_service.analytics_client.get_search_positions',AsyncMock(return_value={})), \
             patch('analysis_service.stock_history_client.get_stock_for_day',AsyncMock(return_value={123:10})):
            report=await build_cabinet_report('spb_tt','Fixture',15,'percent',date(2026,10,20))
        self.assertEqual(len(report.rows),2)
        self.assertTrue(all(r.fbo_stock_yesterday is None and r.views_first_7 is None for r in report.rows))

    async def test_http_429_retries_then_keeps_explicit_failure(self):
        import aiohttp
        class Response:
            status=429
            headers={'Retry-After':'21'}
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def text(self): return 'rate limited'
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def post(self,*args,**kwargs): return Response()
        client=AnalyticsClient()
        client._pace=AsyncMock()
        with patch.object(aiohttp,'ClientTimeout',lambda **kwargs:None,create=True), \
             patch.object(aiohttp,'ClientSession',lambda **kwargs:Session(),create=True), \
             patch('analytics_client.asyncio.sleep',AsyncMock()) as sleep:
            with self.assertRaisesRegex(AnalyticsError,'429'):
                await client._post_json('spb_tt','/fixture',{})
        self.assertEqual(sleep.await_count,2)


if __name__ == '__main__':
    unittest.main()

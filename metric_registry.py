"""Metric definitions are part of every exported report, not implicit code knowledge."""
from datetime import date

CHECKED='2026-10-20'
DOC='https://dev.wildberries.ru/docs/openapi/'

# id, Russian label, unit, endpoint.field, grain, time, aggregation, conditions, limitation
METRICS=[
('ordered_units','Заказанные единицы','шт.','/api/v1/supplier/orders: одна строка с уникальным srid','cabinet,nm,day','date, Europe/Moscow','SUM','Статистика','Оперативные оплаченные заказы; не все способы оплаты; отмены включены'),
('order_events','Заказы как корзины/события','событие','нет надёжного отдельного контракта','cabinet,nm,day','НД','NOT_SUPPORTED','Дополнительный источник','srid идентифицирует единицу, не корзину из нескольких товаров'),
('order_amount','Сумма заказов по цене продавца','RUB','orders.priceWithDisc','cabinet,nm,day','дата заказа','SUM по единицам','Статистика','Не прибыль, не окончательная финансовая выручка; валюта RUB требует подтверждения страны кабинета'),
('buyer_order_amount','Сумма по цене покупателя','RUB','orders.finishedPrice','cabinet,nm,day','дата заказа','SUM по единицам','Статистика','Не смешивается с priceWithDisc'),
('cancel_units','Отменённые заказанные единицы','шт.','orders.isCancel','cabinet,nm,order_day','отнесено к дате заказа; cancelDate отдельно','SUM','Статистика','Ретроспективно пересматривается, не число отмен дня'),
('sales_units','Проданные единицы','шт.','/api/v1/supplier/sales.saleID S','cabinet,nm,event_day','date, Europe/Moscow','SUM','Статистика','Оперативный источник, не финансы'),
('return_units','Возвращённые единицы','шт.','/api/v1/supplier/sales.saleID R','cabinet,nm,event_day','date, Europe/Moscow','SUM','Статистика','Не вычитаются из заказов; разные даты'),
('offer_price','Цена предложения после скидки продавца','currency','/api/v2/list/goods/filter.sizes.discountedPrice','cabinet,nm,size,snapshot','время запроса, не точное время изменения','LAST snapshot','Цены и скидки','Истории до начала накопления нет; не персональная цена покупателя'),
('seller_discount','Скидка продавца','доля','goods.discount / 100','cabinet,nm,snapshot','время запроса','LAST','Цены и скидки','WB-скидка отдельно, из этой метрики не выводится'),
('stock_available','Остаток источника','шт.','/api/analytics/v1/stocks-report/{wb,seller}-warehouses.quantity','cabinet,nm,chrt,warehouse,model,snapshot','срез на момент запроса, источник обновляется ~30 мин','SUM внутри одного среза','Аналитика','Не доказывает возможность покупки во всех регионах; отсутствие строки = MISSING'),
('stock_historical','Исторический остаток','шт.','STOCK_HISTORY_DAILY_CSV.<date>','cabinet,nm,chrt,office,day','23:59 Europe/Moscow','SUM внутри даты, не между датами','Аналитика','До 3 месяцев; квота CSV, не более 20 отчётов/день'),
('views','Переходы в карточку','события','/api/analytics/v3/sales-funnel/products/history.history.openCount','cabinet,nm,day','date источника, Europe/Moscow','SUM','Аналитика','Дневная история последней недели; до 20 nmIds/запрос; 3 запроса/мин для Personal/Service'),
('carts','Добавления в корзину','события','history.cartCount','cabinet,nm,day','как views','SUM','Аналитика','События разных покупательских путей'),
('funnel_orders','Заказы воронки','шт.','history.orderCount','cabinet,nm,day','дата заказа в Analytics','SUM','Аналитика','Не подменяется заказами другого источника'),
('wishlist','Добавления в избранное','события','history.addToWishlistCount','cabinet,nm,day','как views','SUM','Аналитика','Пустое поле не превращается в 0'),
('funnel_buyout_units','Выкупы воронки по дате заказа','шт.','history.buyoutCount','cabinet,nm,order_day','Дата заказа; не дата выкупа','SUM','Аналитика','Пересматриваются; не складывать с sales_units'),
('product_rating','Рейтинг карточки','балл','products.product.productRating','cabinet,nm,snapshot','Текущее значение на момент наблюдения','LAST','Аналитика','Не восстанавливает рейтинг на историческую дату'),
('feedback_rating','Оценка отзывов','балл','products.product.feedbackRating','cabinet,nm,snapshot','Текущее значение на момент наблюдения','LAST','Аналитика','Не количество отзывов'),
('feedback_count','Количество отзывов','шт.','Не подключён отдельный источник в этом выпуске','cabinet,nm,snapshot','НД','LAST','Нужен проверенный источник/ручной агрегированный импорт','Не собирать персональные сведения; адаптер пока не реализован'),
('negative_feedback','Агрегированные негативные отзывы','шт.','Не подключён отдельный источник в этом выпуске','cabinet,nm,period','НД','SUM только по объявленной области','Нужен проверенный источник/ручной агрегированный импорт','Адаптер пока не реализован; отсутствие данных не означает отсутствие жалоб'),
('card_blocked','Блокировка карточки','флаг','Нет подключённого авторитетного статуса блокировки','cabinet,nm,snapshot','НД','LAST','Нужен отдельный подтверждённый источник','TRASH/ACTIVE не означают автоматически блокировку/доступность покупки'),
('delivery_time','Время доставки по определению WB','days,hours,mins','statistic.selected/past.timeToReady','cabinet,nm,period','Среднее за запрошенный период','Значение WB','Аналитика','Не обещанный срок для региона покупателя'),
('ads_budget_bids','Бюджеты и ставки рекламы','разные','В этом выпуске не подключены; v1/budget объявлен устаревающим WB','cabinet,campaign,snapshot','НД','LAST','Нужен поддерживаемый read-only источник','Сбор текущих расходов и статусов работает отдельно; автоматических изменений нет'),
('cart_rate','Отношение корзин к просмотрам','доля','SUM(carts)/SUM(views)','cabinet,nm,period','одинаковый завершённый период','RATIO_OF_SUMS','Совместимая воронка','Не когортная вероятность; знаменатель 0 = НД'),
('order_rate','Отношение заказов к корзинам','доля','SUM(funnel_orders)/SUM(carts)','cabinet,nm,period','одинаковый завершённый период','RATIO_OF_SUMS','Совместимая воронка','Не среднее дневных процентов'),
('impressions','Показы общей карточки','показ','нет проверенного общего источника','cabinet,nm,day','НД','NOT_SUPPORTED','Отдельный источник','Просмотры не заменяют показы; CTR не вычисляется'),
('search_position','Средняя позиция в поиске','позиция','/api/v2/search-report/table/details.avgPosition.current','cabinet,nm,period,query_scope','период запроса','значение WB; между nmId без весов не агрегируется','Аналитика, Джем при наличии','Состав запросов может меняться; не позиция фиксированного запроса'),
('ads_spend','Расход рекламы','currency','GET /adv/v3/fullstats.days.apps.nms.sum; ручной импорт отдельно','cabinet,campaign,nm,day,app,attribution','дата источника; окно атрибуции отдельно','SUM платформ/строк при сверке с итогом кампании','Продвижение','Точное окно атрибуции требует проверки; права не проверены до запуска'),
('margin','Подтверждённая удельная маржа','RUB/unit','Economics.unit_margin','cabinet,nm,valid_from','период ручного подтверждения','не суммировать проценты','Ручной импорт','Без учёта всех затрат прибыльность не утверждается'),
('competitors','Наблюдения конкурентов','разные','CompetitorSnapshots: подтверждённый ручной импорт','competitor_nm,snapshot','время наблюдения','LAST','Ручной подтверждённый источник','Без парсинга и платных сервисов'),
('card_events','Изменения карточки','событие','хеш разрешённых полей Content при наблюдении','cabinet,nm,observed_at','момент обнаружения изменения','список событий','Контент','Не точное время изменения; отзывы покупателей не экспортируются'),
('coverage_days','Покрытие истории','дней','v3_coverage','cabinet,source,period','даты успешной полной синхронизации','COUNT','Успешная пагинация до конца','OK не означает доказанность бизнес-интерпретации'),
]


def registry(cabinets, source_status):
    latest={}
    for x in source_status:
        identity=(x['cabinet_id'],x['source'])
        if x.get('is_latest',True) or identity not in latest:latest[identity]=x
    rows=[]
    for m in METRICS:
        mid,label,unit,source,grain,time,agg,conditions,limits=m
        channel=('orders' if mid in {'ordered_units','order_amount','buyer_order_amount','cancel_units'} else
                 'sales' if mid in {'sales_units','return_units'} else
                 'prices' if mid in {'offer_price','seller_discount'} else
                 'stocks_wb' if mid=='stock_available' else 'stock_history_wb' if mid=='stock_historical' else
                 'funnel' if mid in {'views','carts','funnel_orders','wishlist','funnel_buyout_units','cart_rate','order_rate'} else
                 'funnel_period' if mid in {'product_rating','feedback_rating','delivery_time'} else
                 'search' if mid=='search_position' else 'catalog' if mid=='card_events' else mid)
        if mid=='ads_spend':channel='ads'
        for cabinet in cabinets:
            record=latest.get((cabinet,channel),{})
            status=record.get('status','MISSING')
            reason=record.get('reason','Фактическая доступность не проверена; нужны данные кабинета или ручной импорт')
            if mid in {'impressions','order_events'}: status,reason='NOT_SUPPORTED',limits
            rows.append(dict(metric_id=mid,russian_name=label,unit=unit,source=source,source_id=channel,grain=grain,
                             time_semantics=time,aggregation=agg,requirements=conditions,limits=limits,
                             cabinet_id=cabinet,status=status,reason=reason,checked_on=CHECKED,
                             documentation=DOC+('reports' if channel in {'orders','sales'} else 'item-management' if channel in {'catalog','prices'} else 'promotion' if channel=='ads' else 'analytics'),
                             missing_policy='NULL + отдельный статус; 0 только по полному контракту',
                             availability_meaning='Статус источника; полнота каждого значения указана в построчных статусах и покрытии',
                             collection_method='API' if channel in {'orders','sales','prices','funnel','funnel_period','search','catalog','stocks_wb','stock_history_wb','ads'} else 'MANUAL_OR_UNAVAILABLE'))
            if mid in {'stock_available','stock_historical'}:
                rows[-1]['model']='FBO'
                other=dict(rows[-1]);other.update(metric_id=mid+'_fbs',model='FBS',source_id=channel.replace('_wb','_mp'))
                record=latest.get((cabinet,other['source_id']),{})
                other.update(status=record.get('status','MISSING'),reason=record.get('reason','Права и данные FBS пока не проверены'))
                rows.append(other)
    return rows

TABLES={
 'Products':'cabinet_id + nm_id', 'ProductDaily':'cabinet_id + nm_id + day',
 'CabinetComparison':'canonical_product_id + report_date (две подтверждённые карточки)',
 'VariantCatalog':'cabinet_id + nm_id + variant_id + barcode',
 'VariantDaily':'cabinet_id + nm_id + barcode + size + model + day',
 'PriceHistory':'cabinet_id + nm_id + size_id + snapshot_at + price_type',
 'PriceComparisons':'cabinet_id + nm_id + size_id + price_type + requested_day',
 'StockSnapshots':'cabinet_id + source + observation_key + snapshot_at (warehouse_id may be absent in CSV; office name retained)',
 'AdsDaily':'cabinet_id + campaign_id + nm_id + day + app_type + attribution_window',
 'CampaignSnapshots':'cabinet_id + campaign_id + snapshot_at',
 'CampaignEvents':'event_id (изменение наблюдаемого статуса кампании)',
 'CampaignProducts':'cabinet_id + campaign_id + nm_id + valid_from',
 'SearchDaily':'cabinet_id + nm_id + period_start + period_end + query_scope',
 'FunnelPeriods':'cabinet_id + nm_id + period_start + period_end + snapshot_at',
 'CardMetricSnapshots':'cabinet_id + nm_id + snapshot_at',
 'CardEvents':'cabinet_id + nm_id + observed_at + event_id',
 'CompetitorSnapshots':'competitor_nm_id + snapshot_at + source',
 'ProductMapping':'canonical_product_id + cabinet_id + nm_id + variant_id',
 'Economics':'cabinet_id + nm_id + valid_from', 'Evidence':'evidence_id', 'Actions':'action_id',
 'AvailabilityPeriods':'cabinet_id + nm_id + period_start + period_end + scope',
 'SourceStatus':'run_id + cabinet_id + source', 'MetricRegistry':'metric_id + cabinet_id',
 'PeriodMetrics':'cabinet_id + nm_id + window_days + period_start + previous_start',
}

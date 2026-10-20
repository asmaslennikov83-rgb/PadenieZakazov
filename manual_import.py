"""Explicitly sourced imports, validated before any write; no account auto-matching."""
from datetime import date,datetime
from pathlib import Path
import csv
import json
from collector import SourceError
from data_quality import nm_id, number
from history_store import dumps,key,now

SCHEMAS={
'ProductMapping':(['canonical_product_id','cabinet_id','nm_id','variant_id','units_per_item','comparison_unit','mapping_source','mapping_status'],['canonical_product_id','cabinet_id','nm_id','variant_id']),
'Economics':(['cabinet_id','nm_id','valid_from','unit_margin','source','status'],['cabinet_id','nm_id','valid_from']),
'AvailabilityPeriods':(['cabinet_id','nm_id','period_start','period_end','scope','availability_status','source','status'],['cabinet_id','nm_id','period_start','period_end','scope']),
'AdsDaily':(['cabinet_id','campaign_id','nm_id','day','spend','impressions','clicks','attributed_orders','attributed_amount','attribution_window','source','status'],['cabinet_id','campaign_id','nm_id','day','attribution_window']),
'CampaignProducts':(['cabinet_id','campaign_id','nm_id','valid_from','source','status'],['cabinet_id','campaign_id','nm_id','valid_from']),
'CompetitorSnapshots':(['competitor_nm_id','snapshot_at','source','price','currency','rating','status'],['competitor_nm_id','snapshot_at','source']),
}
STATUSES={'OK','PARTIAL','MISSING','STALE','ERROR','NOT_SUPPORTED','NO_ACCESS','NOT_APPLICABLE'}


def validate(kind,rows):
    if kind not in SCHEMAS:raise ValueError('Неизвестная таблица импорта')
    fields,keys=SCHEMAS[kind];result=[];seen=set()
    for row in rows:
        if set(row)!=set(fields):raise ValueError(f'{kind}: ожидались ровно поля {fields}')
        row={k:(str(v).strip() if v is not None else '') for k,v in row.items()}
        if 'cabinet_id' in row and row['cabinet_id'] not in {'spb_tt','spb_tt_group'}:raise ValueError('Неизвестный кабинет')
        for f in ('nm_id','competitor_nm_id'):
            if f in row:
                n=nm_id(row[f])
                if n is None:raise ValueError('Некорректный nmId')
                row[f]=str(n)
        for f in ('day','valid_from','period_start','period_end'):
            if f in row:date.fromisoformat(row[f])
        if 'snapshot_at' in row:
            dt=datetime.fromisoformat(row['snapshot_at'])
            if dt.tzinfo is None:raise ValueError('Укажите часовой пояс снимка')
        for f in ('spend','impressions','clicks','attributed_orders','attributed_amount','price','rating','units_per_item','unit_margin'):
            if f in row:
                raw=row[f]
                if f=='unit_margin':
                    try:val=float(raw.replace(',','.'))
                    except ValueError:raise ValueError('Нужна числовая маржа')
                    if not __import__('math').isfinite(val):raise ValueError('Некорректная маржа')
                else:val=number(raw)
                if raw and val is None:raise ValueError(f'Некорректное числовое поле {f}')
                row[f]=val
        if kind=='ProductMapping':
            if row['mapping_status']!='CONFIRMED' or not row['mapping_source'] or not row['comparison_unit'] or not row['units_per_item'] or row['units_per_item']<=0:raise ValueError('Нужно подтверждённое соответствие, единица и комплектность >0')
        elif row['status'] not in STATUSES:raise ValueError('Неизвестный статус')
        if kind=='AvailabilityPeriods' and (row['availability_status'] not in {'CONFIRMED_AVAILABLE','CONFIRMED_UNAVAILABLE'} or not row['scope'] or row['period_start']>row['period_end']):raise ValueError('Нужны подтверждённая доступность, область и корректный период')
        if 'source' in row and not row['source']:raise ValueError('Укажите источник ручного наблюдения')
        if kind=='AdsDaily' and (not row['attribution_window'] or not row['campaign_id']):raise ValueError('Нужно окно атрибуции и ID кампании; распределите расход по SKU без дублей')
        identity=key(*(row[k] for k in keys))
        if identity in seen:raise ValueError('Повтор ключа в файле импорта')
        seen.add(identity);result.append((identity,row))
    return result


def import_rows(store,kind,rows):
    validated=validate(kind,rows)
    # Distinct confirmed product IDs may not claim the same cabinet/card/variant.
    if kind=='ProductMapping':
        old=[json.loads(r['payload']) for r in store.rows("SELECT payload FROM v3_manual WHERE kind='ProductMapping'")]
        merged={key(r['canonical_product_id'],r['cabinet_id'],r['nm_id'],r['variant_id']):r for r in old}
        merged.update(dict(validated));pairs={}
        for r in merged.values():
            identity=(r['cabinet_id'],r['nm_id'],r['variant_id'])
            if identity in pairs and pairs[identity]!=r['canonical_product_id']:raise ValueError('Один вариант сопоставлен нескольким canonical_product_id')
            pairs[identity]=r['canonical_product_id']
    with store.connect() as db:
        for identity,row in validated:db.execute('INSERT OR REPLACE INTO v3_manual VALUES(?,?,?,?)',(kind,identity,dumps(row),now()))
    return len(validated)


def import_csv(store,kind,path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:return import_rows(store,kind,list(csv.DictReader(f,delimiter=';')))

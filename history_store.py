"""Versioned local facts. No buyer data or API credentials are stored here."""
from __future__ import annotations
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import gzip
import json
import sqlite3
import uuid
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 3


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def key(*parts):
    return hashlib.sha256(dumps(parts).encode()).hexdigest()[:32]


MIGRATION = '''
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v3_products(cabinet_id TEXT,nm_id TEXT,payload TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,PRIMARY KEY(cabinet_id,nm_id));
CREATE TABLE IF NOT EXISTS v3_events(cabinet_id TEXT,source TEXT,event_key TEXT,day TEXT,nm_id TEXT,payload TEXT NOT NULL,version_at TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(cabinet_id,source,event_key));
CREATE INDEX IF NOT EXISTS v3_events_period ON v3_events(cabinet_id,source,day,nm_id);
CREATE TABLE IF NOT EXISTS v3_coverage(cabinet_id TEXT,source TEXT,day TEXT,status TEXT NOT NULL,run_id TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(cabinet_id,source,day));
CREATE TABLE IF NOT EXISTS v3_sync(cabinet_id TEXT,source TEXT,payload TEXT NOT NULL,PRIMARY KEY(cabinet_id,source));
CREATE TABLE IF NOT EXISTS v3_sources(run_id TEXT,cabinet_id TEXT,source TEXT,payload TEXT NOT NULL,PRIMARY KEY(run_id,cabinet_id,source));
CREATE TABLE IF NOT EXISTS v3_observations(cabinet_id TEXT,source TEXT,observation_key TEXT,snapshot_at TEXT,payload TEXT NOT NULL,PRIMARY KEY(cabinet_id,source,observation_key,snapshot_at));
CREATE TABLE IF NOT EXISTS v3_funnel(cabinet_id TEXT,nm_id TEXT,day TEXT,payload TEXT NOT NULL,run_id TEXT,observed_at TEXT,PRIMARY KEY(cabinet_id,nm_id,day));
CREATE TABLE IF NOT EXISTS v3_manual(kind TEXT,row_key TEXT,payload TEXT NOT NULL,imported_at TEXT NOT NULL,PRIMARY KEY(kind,row_key));
CREATE TABLE IF NOT EXISTS v3_evidence(evidence_id TEXT PRIMARY KEY,payload TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v3_actions(action_id TEXT PRIMARY KEY,evidence_id TEXT,status TEXT NOT NULL,owner TEXT,due_date TEXT,completed_at TEXT,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v3_action_history(id INTEGER PRIMARY KEY AUTOINCREMENT,action_id TEXT,changed_at TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS v3_preferences(user_id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v3_reports(run_id TEXT PRIMARY KEY,report_date TEXT,revision INTEGER,created_at TEXT,payload TEXT,UNIQUE(report_date,revision));
CREATE TABLE IF NOT EXISTS v3_deliveries(delivery_key TEXT PRIMARY KEY,status TEXT NOT NULL,created_at TEXT,updated_at TEXT,message_id TEXT);
CREATE TABLE IF NOT EXISTS v3_leases(name TEXT PRIMARY KEY,owner TEXT NOT NULL,expires_at TEXT NOT NULL);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def backup(self):
        dest = self.path.parent / 'backups' / f'{self.path.stem}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}.sqlite'
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, closing(sqlite3.connect(dest)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Резервная копия не прошла integrity_check')
        return dest

    def migrate(self):
        exists = self.path.exists()
        with self.connect() as db:
            has = db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone()
            if has and db.execute('SELECT 1 FROM schema_migrations WHERE version=?',(SCHEMA_VERSION,)).fetchone():
                return None
        backup = self.backup() if exists else None
        with self.connect() as db:
            db.executescript('BEGIN IMMEDIATE;\n' + MIGRATION)
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (?,?)',(SCHEMA_VERSION,now()))
        return backup

    def rows(self, query, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(query,params)]

    def source(self, run, cabinet, source, **meta):
        meta.update(run_id=run,cabinet_id=cabinet,source=source)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO v3_sources VALUES(?,?,?,?)',(run,cabinet,source,dumps(meta)))

    def sources(self, run=None):
        if run:
            rows=self.rows('SELECT payload FROM v3_sources WHERE run_id=?',(run,))
        else:
            rows=self.rows('SELECT payload FROM v3_sources ORDER BY rowid')
        return [json.loads(x['payload']) for x in rows]

    def product(self, db, cabinet, n, attributes, observed):
        old=db.execute('SELECT payload FROM v3_products WHERE cabinet_id=? AND nm_id=?',(cabinet,n)).fetchone()
        payload=json.loads(old[0]) if old else {}
        attributes=dict(attributes)
        if payload.get('life_status') in {'ACTIVE','TRASH'} and attributes.get('life_status')=='OBSERVED_ACTIVITY':attributes.pop('life_status')
        if attributes.get('first_activity_date') and payload.get('first_activity_date'):
            attributes['first_activity_date']=min(attributes['first_activity_date'],payload['first_activity_date'])
        payload.update({k:v for k,v in attributes.items() if v is not None and v != ''})
        db.execute('INSERT INTO v3_products VALUES(?,?,?,?,?) ON CONFLICT(cabinet_id,nm_id) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen',
                   (cabinet,n,dumps(payload),observed,observed))

    def events(self, run, cabinet, source, records):
        stamp=now()
        with self.connect() as db:
            for record in records:
                n=record['nm_id']
                self.product(db,cabinet,n,{**record.get('product',{}),'first_activity_date':record['day']},stamp)
                db.execute('INSERT INTO v3_events VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cabinet_id,source,event_key) DO UPDATE SET day=excluded.day,nm_id=excluded.nm_id,payload=excluded.payload,version_at=excluded.version_at,run_id=excluded.run_id',
                           (cabinet,source,record['event_key'],record['day'],n,dumps(record),stamp,run))

    def cover(self, run, cabinet, source, start, end):
        with self.connect() as db:
            day=start
            while day<=end:
                db.execute('INSERT OR REPLACE INTO v3_coverage VALUES(?,?,?,?,?,?)',(cabinet,source,str(day),'OK',run,now()))
                day+=timedelta(days=1)

    def sync_state(self, cabinet, source, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO v3_sync VALUES(?,?,?)',(cabinet,source,dumps(value)))
                return value
            row=db.execute('SELECT payload FROM v3_sync WHERE cabinet_id=? AND source=?',(cabinet,source)).fetchone()
            return json.loads(row[0]) if row else {}

    def observations(self, cabinet, source, records, snapshot_at):
        with self.connect() as db:
            for record in records:
                stamp=snapshot_at or record['snapshot_at']
                parsed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                if parsed.tzinfo is None:raise ValueError('Время снимка без часового пояса')
                stamp=parsed.astimezone(ZoneInfo('Europe/Moscow')).isoformat()
                record={**record,'snapshot_at':stamp}
                n=record.get('nm_id')
                if n is not None:self.product(db,cabinet,n,record.get('product',{}),now() if source.startswith('stock_history_') else stamp)
                db.execute('INSERT OR REPLACE INTO v3_observations VALUES(?,?,?,?,?)',
                           (cabinet,source,record['observation_key'],stamp,dumps(record)))

    def preference(self, user_id, values=None):
        defaults={'min_base':0,'min_absolute':0,'repeat_days':1,'rare_mode':'separate','growth_threshold':0.15,'stock_days':7,'urgent':False}
        with self.connect() as db:
            row=db.execute('SELECT payload FROM v3_preferences WHERE user_id=?',(user_id,)).fetchone()
            defaults.update(json.loads(row[0]) if row else {})
            if values is not None:
                if not set(values)<=set(defaults): raise ValueError('Неизвестная настройка')
                defaults.update(values)
                db.execute('INSERT OR REPLACE INTO v3_preferences VALUES(?,?)',(user_id,dumps(defaults)))
        return defaults

    def lease(self, name, seconds=300):
        owner=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT expires_at FROM v3_leases WHERE name=?',(name,)).fetchone()
            if row and row[0]>now(): return None
            db.execute('INSERT OR REPLACE INTO v3_leases VALUES(?,?,?)',(name,owner,(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()))
        return owner

    def release(self, name, owner):
        with self.connect() as db: db.execute('DELETE FROM v3_leases WHERE name=? AND owner=?',(name,owner))

    def delivery(self, delivery_key, status=None, message_id=None):
        with self.connect() as db:
            if status is None:
                cur=db.execute("INSERT OR IGNORE INTO v3_deliveries VALUES(?,'SENDING',?,?,NULL)",(delivery_key,now(),now()))
                return cur.rowcount==1
            db.execute('UPDATE v3_deliveries SET status=?,updated_at=?,message_id=? WHERE delivery_key=?',(status,now(),str(message_id) if message_id else None,delivery_key))

    def save_report(self, run, day, payload):
        fingerprint=key(payload['summary'],payload['all_products'],payload['tables'],payload.get('comparisons'))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT payload,run_id,revision,created_at FROM v3_reports WHERE report_date=? ORDER BY revision DESC LIMIT 1',(str(day),)).fetchone()
            if prior and json.loads(prior[0]).get('fingerprint')==fingerprint:
                payload.update(revision=prior['revision'],run_id=prior['run_id'],generated_at=prior['created_at'],report_date=str(day))
                return payload
            revision=db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM v3_reports WHERE report_date=?',(str(day),)).fetchone()[0]
            payload.update(revision=revision,run_id=run,generated_at=now(),report_date=str(day))
            directory=self.path.parent/'report_snapshots';directory.mkdir(parents=True,exist_ok=True)
            path=directory/f'{day}_r{revision}_{run}.json.gz'
            raw=gzip.compress(dumps(payload).encode(),compresslevel=6)
            # Unique run_id and exclusive creation make previous revisions immutable.
            with path.open('xb') as file:file.write(raw)
            meta={'fingerprint':fingerprint,'snapshot_path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'status':payload['status'],'summary':payload['summary']}
            db.execute('INSERT INTO v3_reports VALUES(?,?,?,?,?)',(run,str(day),revision,payload['generated_at'],dumps(meta)))
        return payload

    def record_evidence(self, evidence):
        stamp=now()
        with self.connect() as db:
            for item in evidence:
                eid=item['evidence_id']
                db.execute('INSERT INTO v3_evidence VALUES(?,?,?,?) ON CONFLICT(evidence_id) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen',
                           (eid,dumps(item),stamp,stamp))
                aid=key('action',eid)
                db.execute("INSERT OR IGNORE INTO v3_actions VALUES(?,?,'OPEN',NULL,?,NULL,?,?,?)",(aid,eid,item['review_date'],dumps({'next_action':item['next_action'],'metric_success':item['metric_success'],'cabinet_id':item['cabinet_id'],'nm_id':item['nm_id']}),stamp,stamp))

    def action(self, action_id, status, owner=None, due_date=None):
        if status not in {'OPEN','IN_PROGRESS','DONE','CANCELLED'}: raise ValueError('Неизвестный статус действия')
        if due_date: datetime.fromisoformat(due_date)
        with self.connect() as db:
            row=db.execute('SELECT * FROM v3_actions WHERE action_id=?',(action_id,)).fetchone()
            if not row: raise ValueError('Действие не найдено')
            db.execute('INSERT INTO v3_action_history(action_id,changed_at,payload) VALUES(?,?,?)',(action_id,now(),dumps(dict(row))))
            db.execute('UPDATE v3_actions SET status=?,owner=COALESCE(?,owner),due_date=COALESCE(?,due_date),completed_at=?,updated_at=? WHERE action_id=?',
                       (status,owner,due_date,now() if status=='DONE' else None,now(),action_id))

    def prune_diagnostics(self, days=14):
        cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        with self.connect() as db:
            db.execute('''DELETE FROM v3_sources WHERE json_extract(payload,'$.requested_at')<?
                AND run_id NOT IN (SELECT run_id FROM v3_events UNION SELECT run_id FROM v3_coverage
                UNION SELECT run_id FROM v3_funnel UNION SELECT json_extract(payload,'$.run_id') FROM v3_observations WHERE json_extract(payload,'$.run_id') IS NOT NULL)''',(cutoff,))
        # Fact history and report revisions are retained; never fabricate older snapshots.

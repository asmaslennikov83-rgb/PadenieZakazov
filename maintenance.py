"""Local administration. Never sends credentials/data to Telegram or other services."""
import argparse
from contextlib import closing
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import sqlite3
from config import DB_PATH
from history_store import Store
from manual_import import import_csv,SCHEMAS


def restore(store,path):
    from runtime_lock import instance_lock
    with instance_lock(store.path.parent/'bot.lock'):
        return _restore_stopped(store,path)


def _restore_stopped(store,path):
    path=Path(path).resolve()
    if not path.is_file() or path==store.path.resolve():raise ValueError('Укажите отдельную резервную копию')
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)) as src:
        if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Повреждена резервная копия')
        backup=store.backup()
        with store.connect() as dst:src.backup(dst)
    return backup


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default=str(DB_PATH));sub=parser.add_subparsers(dest='command',required=True)
    for cmd in ('backup','migrate','health','templates'):sub.add_parser(cmd)
    p=sub.add_parser('restore');p.add_argument('path');p.add_argument('--bot-stopped',action='store_true',required=True)
    p=sub.add_parser('import');p.add_argument('table',choices=SCHEMAS);p.add_argument('path')
    p=sub.add_parser('action');p.add_argument('id');p.add_argument('status');p.add_argument('--owner');p.add_argument('--due')
    p=sub.add_parser('export');p.add_argument('--date',default=(datetime.now(ZoneInfo('Europe/Moscow')).date()-timedelta(days=1)).isoformat());p.add_argument('--out',default='data/reports')
    args=parser.parse_args();store=Store(args.db)
    if args.command=='restore':print('Предыдущая база сохранена:',restore(store,args.path));return
    backup=store.migrate()
    if args.command=='migrate':print('Миграция завершена. Резервная копия:',backup)
    elif args.command=='backup':print(store.backup())
    elif args.command=='health':
        with store.connect() as db:print('integrity_check:',db.execute('PRAGMA integrity_check').fetchone()[0])
        print('Источники:',json.dumps(store.sources()[-12:],ensure_ascii=False,indent=2))
    elif args.command=='templates':
        directory=Path('data/import_templates');directory.mkdir(parents=True,exist_ok=True)
        for name,(fields,_) in SCHEMAS.items():(directory/(name+'.csv')).write_text(';'.join(fields)+'\n',encoding='utf-8-sig')
        print(directory)
    elif args.command=='import':print('Импортировано:',import_csv(store,args.table,args.path))
    elif args.command=='action':store.action(args.id,args.status,args.owner,args.due);print('Действие обновлено вручную')
    elif args.command=='export':
        from business_report import build
        from analytical_export import build_package
        report=build(store,date.fromisoformat(args.date),['spb_tt','spb_tt_group'])
        name,data=build_package(report);out=Path(args.out);out.mkdir(parents=True,exist_ok=True);(out/name).write_bytes(data);print(out/name)


if __name__=='__main__':main()

"""Bounded diagnostic log with stage names only, no responses or credentials."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from history_store import now


def stage(path,operation,stage_name,**fields):
    logger=logging.getLogger('wb.runtime.stages')
    if not logger.handlers:
        target=Path(path).parent/'logs'/'runtime.log';target.parent.mkdir(parents=True,exist_ok=True)
        handler=RotatingFileHandler(target,maxBytes=512000,backupCount=2,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'));logger.addHandler(handler);logger.setLevel(logging.INFO)
    payload={'at':now(),'pid':os.getpid(),'operation':operation,'stage':stage_name,**fields}
    logger.info(json.dumps(payload,ensure_ascii=False))

"""Bounded diagnostic history in the existing database, plus JSON stdout logs."""
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone

from fns_mcp.redaction import clean_context

EVENT_LIMIT = 50000
EVENT_DAYS = 30
RUN_LIMIT = 1000
RUN_DAYS = 90
logger = logging.getLogger('fns_mcp.events')
RUN_FIELDS = {'status', 'phase', 'finished_at', 'heartbeat_at', 'seen', 'downloaded',
              'skipped', 'failed', 'pages', 'requests', 'retries', 'last_error', 'last_error_context'}


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


class Journal:
    def __init__(self, store):
        self.store = store
        self.written = 0
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id TEXT PRIMARY KEY, trigger TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, heartbeat_at REAL NOT NULL, status TEXT NOT NULL,
                    phase TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0,
                    downloaded INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, last_error_context TEXT
                );
                CREATE INDEX IF NOT EXISTS sync_runs_started ON sync_runs(started_at);
                CREATE TABLE IF NOT EXISTS sync_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, at REAL NOT NULL,
                    level TEXT NOT NULL, event TEXT NOT NULL, context TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sync_events_run ON sync_events(run_id,id);
                CREATE INDEX IF NOT EXISTS sync_events_at ON sync_events(at);
            ''')

    def event(self, run_id, event, level='info', **context):
        context = clean_context(context)
        now = time.time()
        record = {'timestamp': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                  'level': level, 'event': event, 'run_id': run_id, 'context': context}
        # Emit first: a database write failure must still be visible in Docker logs.
        logger.log({'info':logging.INFO, 'warning':logging.WARNING, 'error':logging.ERROR}[level],
                   json.dumps(record, ensure_ascii=False, allow_nan=False))
        with self.store.connect() as db:
            db.execute('INSERT INTO sync_events(run_id,at,level,event,context) VALUES (?,?,?,?,?)',
                       (run_id, now, level, event, json.dumps(context, ensure_ascii=False, allow_nan=False)))
        self.written += 1
        if self.written % 100 == 0:
            self.prune()

    def start(self, run_id, trigger, started_at):
        with self.store.connect() as db:
            db.execute('INSERT INTO sync_runs(id,trigger,started_at,heartbeat_at,status,phase) VALUES (?,?,?,?,?,?)',
                       (run_id, trigger, started_at, started_at, 'running', 'starting'))
        self.event(run_id, 'sync_started', trigger=trigger)
        self.prune()

    def progress(self, run_id, **fields):
        if not fields or not set(fields) <= RUN_FIELDS:
            raise ValueError('Invalid run update')
        if 'last_error_context' in fields and fields['last_error_context'] is not None:
            fields['last_error_context'] = json.dumps(clean_context(fields['last_error_context']), ensure_ascii=False)
        columns = ','.join(name + '=?' for name in fields)
        with self.store.connect() as db:
            db.execute('UPDATE sync_runs SET ' + columns + ' WHERE id=?', [*fields.values(), run_id])

    def recover(self):
        now = time.time()
        with self.store.connect() as db:
            ids = [r['id'] for r in db.execute("SELECT id FROM sync_runs WHERE status='running'")]
            db.execute("UPDATE sync_runs SET status='interrupted', phase='interrupted', finished_at=?, "
                       "last_error='process_restarted' WHERE status='running'", (now,))
        for run_id in ids:
            self.event(run_id, 'sync_interrupted', 'warning', reason='process_restarted')
        return ids

    def prune(self):
        now = time.time()
        with self.store.connect() as db:
            db.execute('DELETE FROM sync_events WHERE at<?', (now-EVENT_DAYS*86400,))
            db.execute('DELETE FROM sync_events WHERE id IN '
                       '(SELECT id FROM sync_events ORDER BY id DESC LIMIT -1 OFFSET ?)', (EVENT_LIMIT,))
            db.execute("DELETE FROM sync_runs WHERE status!='running' AND started_at<?", (now-RUN_DAYS*86400,))
            db.execute("DELETE FROM sync_runs WHERE status!='running' AND id IN "
                       '(SELECT id FROM sync_runs ORDER BY started_at DESC LIMIT -1 OFFSET ?)', (RUN_LIMIT,))

    def runs(self, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?', (limit,))]
        for row in rows:
            row['last_error_context'] = json.loads(row['last_error_context']) if row['last_error_context'] else None
        return {'runs':rows, 'retention_days':RUN_DAYS, 'max_runs':RUN_LIMIT}

    def events(self, run_id=None, level=None, limit=50, before_id=None):
        if not 1 <= limit <= 200 or (before_id is not None and before_id < 1):
            raise ValueError('limit must be 1..200; before_id must be positive')
        if run_id is not None and not re.fullmatch(r'[a-f0-9]{32}', run_id):
            raise ValueError('Invalid run_id')
        if level is not None and level not in ('info','warning','error'):
            raise ValueError('Invalid level')
        where, params = [], []
        for field, value in (('run_id',run_id), ('level',level)):
            if value is not None:
                where.append(field+'=?')
                params.append(value)
        if before_id is not None:
            where.append('id<?')
            params.append(before_id)
        sql = ' WHERE ' + ' AND '.join(where) if where else ''
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM sync_events' + sql + ' ORDER BY id DESC LIMIT ?',
                                               params+[limit])]
        for row in rows:
            row['context'] = json.loads(row['context'])
        return {'events':rows, 'next_before_id':rows[-1]['id'] if rows else None,
                'retention_days':EVENT_DAYS, 'max_events':EVENT_LIMIT}

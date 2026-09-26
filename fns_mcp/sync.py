"""Durable four-hour scheduler. One sync at a time, safe resumption after failure."""

import asyncio
import logging
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from fns_mcp.client import SyncError
from fns_mcp.journal import Journal
from fns_mcp.redaction import clean_context, receipt_ref

INTERVAL = 4 * 60 * 60
logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, store, client_factory):
        self.store, self.client_factory = store, client_factory
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = asyncio.Event()
        self.journal = Journal(store)
        self.scheduler_running = False
        self.persistence_error = False
        interrupted = self.journal.recover()
        if interrupted or self.store.status().get('running'):
            self.store.state(last_error='interrupted', next_sync_at=0)
        self.store.state(running=False, interval_seconds=INTERVAL)
        self.run_id = None
        self.counts = {}

    def status(self):
        result = self.store.status()
        now = time.time()
        heartbeat = result.get('heartbeat_at') if result.get('running') else result.get('scheduler_heartbeat_at')
        result.update(heartbeat_age_seconds=max(0, now-heartbeat) if heartbeat else None,
                      stalled=bool(result.get('running') and (not heartbeat or now-heartbeat > 180)),
                      stale=not result.get('last_success_at') or now-result['last_success_at'] > INTERVAL*2,
                      scheduler_alive=self.scheduler_running,
                      persistence_error=self.persistence_error,
                      pending_receipts=result['receipts']-result['detailed_receipts'],
                      disk_free_bytes=shutil.disk_usage(self.store.path.parent).free)
        runs = self.journal.runs(limit=1)['runs']
        result['latest_run'] = runs[0] if runs else None
        result['warnings'] = [name for name, condition in (
            ('sync_stalled', result['stalled']), ('archive_stale', result['stale']),
            ('sync_error', bool(result.get('last_error'))), ('storage_write_failed', self.persistence_error),
            ('scheduler_not_running', not self.scheduler_running)) if condition]
        return result

    def progress(self, phase):
        now = time.time()
        self.store.state(**self.counts, phase=phase, heartbeat_at=now)
        self.journal.progress(self.run_id, **self.counts, phase=phase, heartbeat_at=now)

    def observe(self, event, **context):
        if event == 'request_started':
            self.counts['requests'] += 1
        elif event == 'request_retry':
            self.counts['retries'] += 1
        elif event == 'page_received':
            self.counts['pages'] += 1
        self.progress(context.get('operation', 'collecting'))
        level = 'warning' if event in ('request_failed', 'request_retry') else 'info'
        self.journal.event(self.run_id, event, level, **context)

    def trigger(self):
        if self.lock.locked() or self.wake.is_set():
            return {"accepted": False, "reason": "already_running_or_queued"}
        self.wake.set()
        return {"accepted": True}

    def run_once(self, trigger='manual'):
        if not self.lock.acquire(blocking=False):
            return
        started = time.time()
        self.run_id = uuid.uuid4().hex
        self.counts = dict.fromkeys(('seen','downloaded','skipped','failed','pages','requests','retries'), 0)
        outcome, error_code, error_context = 'interrupted', None, None
        consecutive_failures = 0
        try:
            self.journal.start(self.run_id, trigger, started)
            self.store.state(running=True, run_id=self.run_id, last_attempt_at=started,
                             last_error=None, last_error_context=None, **self.counts)
            self.progress('authenticating')
            client = self.client_factory()
            client.observe = self.observe
            client.load()
            for receipt in client.receipts():
                self.counts['seen'] += 1
                self.progress('collecting')
                if self.stop.is_set():
                    raise SyncError("interrupted")
                if self.store.has_detail(receipt["key"]):
                    self.counts['skipped'] += 1
                    self.progress('collecting')
                    continue
                self.store.put(receipt, None)
                try:
                    detail = client.detail(receipt["key"])
                    self.store.put(receipt, detail)
                    self.counts['downloaded'] += 1
                    consecutive_failures = 0
                except SyncError as error:
                    self.store.put(receipt, None, str(error))
                    self.counts['failed'] += 1
                    consecutive_failures += 1
                    error_context = clean_context(error.context)
                    self.journal.event(self.run_id, 'receipt_failed', 'error',
                                       **{**error_context, 'receipt_ref':receipt_ref(receipt['key']), 'reason':str(error)})
                    self.progress('collecting')
                    if str(error) in ("authentication_required", "access_denied", "rate_limited", "refresh_temporarily_failed"):
                        raise
                    if consecutive_failures >= 5:
                        raise SyncError('consecutive_failures', context=error_context)
                self.progress('collecting')
            outcome = 'partial' if self.counts['failed'] else 'success'
            error_code = 'partial_failure' if self.counts['failed'] else None
            if not self.counts['failed']:
                self.store.state(last_success_at=time.time())
        except SyncError as error:
            outcome = 'interrupted' if str(error) == 'interrupted' else 'failed'
            error_code, error_context = str(error), clean_context(error.context)
            self.journal.event(self.run_id, 'sync_failed', 'error', reason=error_code, **error_context)
        except Exception as error:
            outcome, error_code = 'failed', 'internal_sync_error'
            error_context = {'exception_type':type(error).__name__, 'frames':' | '.join(
                f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in traceback.extract_tb(error.__traceback__)[-8:])}
            # Keep code locations, never exception text, source lines, or locals.
            logger.error("Receipt sync failed; private exception details suppressed")
            self.journal.event(self.run_id, 'sync_crashed', 'error', **error_context)
        finally:
            try:
                finished = time.time()
                next_sync = max(started + INTERVAL, finished + 60)
                self.journal.progress(self.run_id, **self.counts, status=outcome, phase='finished',
                                      finished_at=finished, heartbeat_at=finished,
                                      last_error=error_code, last_error_context=error_context)
                self.journal.event(self.run_id, 'sync_finished', 'info' if outcome == 'success' else 'warning',
                                   **self.counts, outcome=outcome, reason=error_code, next_sync_at=next_sync)
                self.store.state(running=False, **self.counts, phase='finished', heartbeat_at=finished,
                                 last_finished_at=finished, last_error=error_code,
                                 last_error_context=error_context, next_sync_at=next_sync)
                self.persistence_error = False
            except Exception:
                self.persistence_error = True
                logger.error('Failed to persist sync diagnostics; inspect disk and database health')
                raise
            finally:
                self.lock.release()

    async def schedule(self):
        self.scheduler_running = True
        try:
            while not self.stop.is_set():
                try:
                    self.store.state(scheduler_heartbeat_at=time.time())
                    due = self.store.status().get('next_sync_at', 0)
                    if due > time.time() and not self.wake.is_set():
                        try:
                            await asyncio.wait_for(self.wake.wait(), timeout=min(30, due-time.time()))
                        except asyncio.TimeoutError:
                            continue
                    trigger = 'manual' if self.wake.is_set() else 'scheduled'
                    self.wake.clear()
                    if not self.stop.is_set():
                        await asyncio.to_thread(self.run_once, trigger)
                except Exception:
                    self.persistence_error = True
                    logger.error('Scheduler storage failure; retrying in 60 seconds; private details suppressed')
                    self.wake.clear()
                    if self.stop.is_set():
                        break
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.scheduler_running = False

"""Durable four-hour scheduler. One sync at a time, safe resumption after failure."""

import asyncio
import logging
import threading
import time

from fns_mcp.client import SyncError

INTERVAL = 4 * 60 * 60
logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, store, client_factory):
        self.store, self.client_factory = store, client_factory
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = asyncio.Event()
        self.store.state(running=False, interval_seconds=INTERVAL)

    def trigger(self):
        if self.lock.locked() or self.wake.is_set():
            return {"accepted": False, "reason": "already_running_or_queued"}
        self.wake.set()
        return {"accepted": True}

    def run_once(self):
        if not self.lock.acquire(blocking=False):
            return
        started, downloaded, failed = time.time(), 0, 0
        self.store.state(running=True, last_attempt_at=started, last_error=None)
        try:
            client = self.client_factory()
            client.load()
            for receipt in client.receipts():
                if self.stop.is_set():
                    raise SyncError("interrupted")
                if self.store.has_detail(receipt["key"]):
                    continue
                try:
                    detail = client.detail(receipt["key"])
                    self.store.put(receipt, detail)
                    downloaded += 1
                except SyncError as error:
                    if str(error) in ("authentication_required", "access_denied", "account_action_required", "rate_limited"):
                        raise
                    self.store.put(receipt, None, str(error))
                    failed += 1
            self.store.state(last_success_at=time.time() if not failed else self.store.status().get("last_success_at"),
                             last_error="partial_failure" if failed else None)
        except SyncError as error:
            self.store.state(last_error=str(error))
        except Exception:
            # Never log exception text, request headers, or receipt contents.
            self.store.state(last_error="internal_sync_error")
            logger.error("Receipt sync failed; private exception details suppressed")
        finally:
            self.store.state(running=False, last_finished_at=time.time(), downloaded=downloaded,
                             failed=failed, next_sync_at=max(started + INTERVAL, time.time() + 60))
            self.lock.release()

    async def schedule(self):
        while not self.stop.is_set():
            due = self.store.status().get("next_sync_at", 0)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=max(0, due-time.time()))
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            if not self.stop.is_set():
                await asyncio.to_thread(self.run_once)

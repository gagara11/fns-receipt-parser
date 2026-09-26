import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import export_nalog_receipts as upstream
from fns_mcp.client import FNSClient, SyncError, atomic_json
from fns_mcp.store import Store
from fns_mcp.sync import SyncService
from tests.test_api import FakeResponse, http_error


class ErrorContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        atomic_json(self.path / 'session.json', {'token':'private-token',
                    'refreshToken':'private-refresh', 'sourceDeviceId':'private-device'})
        self.client = FNSClient(self.path / 'session.json', delay=0)
        self.sleep = patch('fns_mcp.client.time.sleep').start()
        self.addCleanup(patch.stopall)

    def test_http_error_retains_structured_code_not_additional_info(self):
        error = upstream.http_error_to_api_error(http_error(422, json.dumps({
            'code':'receipt.unavailable', 'message':'Fiscal data unavailable',
            'additionalInfo':{'phone':'79990000000'}}).encode()), 'private-token')
        self.assertEqual(getattr(error, 'service_code', None), 'receipt.unavailable')
        self.assertEqual(getattr(error, 'service_message', None), 'Fiscal data unavailable')
        self.assertNotIn('79990000000', str(vars(error)))

    def test_unknown_422_is_not_a_claim_about_account_actions(self):
        def reject(*args, **kwargs):
            raise http_error(422, b'{"code":"receipt.unavailable","message":"Try later"}')
        with patch.object(upstream._opener, 'open', side_effect=reject):
            with self.assertRaises(SyncError) as caught:
                self.client.detail('receipt-key')
        self.assertEqual(str(caught.exception), 'fns_rejected_request')

    def test_transient_detail_422_retries_without_refreshing_or_stopping_sync(self):
        with patch.object(upstream._opener, 'open', side_effect=[
            http_error(422, b'{"code":"receipt.unavailable","message":"Try later"}'),
            FakeResponse({'items':[], 'totalSum':100})]):
            self.assertEqual(self.client.detail('receipt-key')['totalSum'], 100)

    def test_authentication_failed_422_refreshes_once_and_persists_rotation(self):
        with patch.object(upstream._opener, 'open', side_effect=[
            http_error(422, b'{"code":"authentication.failed","message":"Invalid token"}'),
            FakeResponse({'token':'rotated-token','refreshToken':'rotated-refresh'}),
            FakeResponse({'receipts':[]})]):
            self.assertEqual(self.client.request('/v1/receipt', {})['receipts'], [])
        self.assertEqual(json.loads((self.path/'session.json').read_text())['refreshToken'], 'rotated-refresh')

    def test_last_error_context_is_redacted_and_keeps_http_and_operation(self):
        body = json.dumps({'code':'receipt.unavailable', 'message':
            'Retry private-token private-refresh private-device secret-key someone@example.com +7 (999) 123-45-67',
            'additionalInfo':{'items':[{'name':'PRIVATE-PURCHASE'}]}}).encode()
        def reject(*args, **kwargs):
            raise http_error(422, body)
        with patch.object(upstream._opener, 'open', side_effect=reject):
            with self.assertRaises(SyncError) as caught:
                self.client.detail('secret-key')
        context = getattr(caught.exception, 'context', {})
        self.assertEqual(context.get('http_status'), 422)
        self.assertEqual(context.get('operation'), 'receipt_detail')
        self.assertEqual(context.get('fns_code'), 'receipt.unavailable')
        serialized = json.dumps(context)
        for value in ('private-token','private-refresh','private-device','secret-key',
                      'someone@example.com','999','PRIVATE-PURCHASE'):
            self.assertNotIn(value, serialized)
        self.assertIn('Retry', context['message'])


class DurableProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = Store(self.path / 'receipts.sqlite3')
        atomic_json(self.path/'session.json', {'token':'private-token'})

    def test_live_progress_is_saved_before_the_next_receipt_finishes(self):
        store = self.store
        class Client:
            def load(self): pass
            def receipts(self):
                yield {'key':'a'}
                yield {'key':'b'}
            def detail(self, key):
                if key == 'b':
                    status = store.status()
                    if status.get('downloaded') != 1 or status.get('seen') != 2:
                        raise AssertionError('Progress was not persisted during the run')
                return {'items':[], 'totalSum':1}
        service = SyncService(self.store, Client)
        service.run_once()
        self.assertEqual(self.store.status()['detailed_receipts'], 2)
        self.assertIsNone(self.store.status()['last_error'])

    def test_auth_failure_does_not_lose_discovered_receipt(self):
        class Client:
            def load(self): pass
            def receipts(self): return [{'key':'pending'}]
            def detail(self, key): raise SyncError('authentication_required')
        SyncService(self.store, Client).run_once()
        self.assertTrue(self.store.get('pending')['found'])
        self.assertEqual(self.store.get('pending')['error'], 'authentication_required')

    def test_detail_422_is_recorded_and_later_receipts_are_still_saved(self):
        client = FNSClient(self.path/'session.json', delay=0)
        def respond(request, timeout):
            payload = json.loads(request.data)
            if request.full_url == upstream.LIST_URL:
                return FakeResponse({'receipts':[{'key':'broken'}, {'key':'good'}], 'hasMore':False})
            if payload['key'] == 'broken':
                raise http_error(422, b'{"code":"receipt.unavailable","message":"Try later"}')
            return FakeResponse({'items':[], 'totalSum':1})
        with patch.object(upstream._opener, 'open', side_effect=respond), patch('fns_mcp.client.time.sleep'):
            SyncService(self.store, lambda:client).run_once()
        self.assertTrue(self.store.has_detail('good'))
        self.assertEqual(self.store.get('broken')['error'], 'fns_rejected_request')
        self.assertEqual(self.store.status()['last_error'], 'partial_failure')

    def test_request_history_survives_reopen_with_redacted_errors_and_retry(self):
        atomic_json(self.path/'session.json', {'token':'private-token', 'refreshToken':'private-refresh'})
        client = FNSClient(self.path/'session.json', delay=0)
        responses = [http_error(503, b'{"code":"temporary.failure","message":"Retry private-refresh user@example.com"}'),
                     FakeResponse({'receipts':[{'key':'secret-receipt'}], 'hasMore':False}),
                     FakeResponse({'items':[], 'totalSum':1})]
        service = SyncService(self.store, lambda:client)
        with patch.object(upstream._opener, 'open', side_effect=responses), patch('fns_mcp.client.time.sleep'), \
                self.assertLogs('fns_mcp.events', level='INFO') as captured:
            service.run_once()
        reopened = SyncService(Store(self.path/'receipts.sqlite3'), lambda:client)
        runs = reopened.journal.runs()['runs']
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]['status'], runs[0]['downloaded'], runs[0]['requests'], runs[0]['retries']),
                         ('success', 1, 3, 1))
        events = reopened.journal.events(run_id=runs[0]['id'])['events']
        error = next(e for e in events if e['event'] == 'request_failed')
        self.assertEqual(error['context']['http_status'], 503)
        self.assertEqual(error['context']['fns_code'], 'temporary.failure')
        self.assertEqual(error['context']['operation'], 'receipt_list')
        serialized = json.dumps(events) + ''.join(r.getMessage() for r in captured.records)
        for value in ('private-token','private-refresh','user@example.com','secret-receipt'):
            self.assertNotIn(value, serialized)
        for record in captured.records:
            self.assertIn('event', json.loads(record.getMessage()))

    def test_restart_marks_unfinished_run_interrupted_without_touching_receipts(self):
        service = SyncService(self.store, lambda:None)
        self.assertTrue(hasattr(service, 'journal'), 'Persistent run journal is missing')
        service.journal.start('a'*32, 'manual', 1000)
        self.store.state(running=True, run_id='a'*32, next_sync_at=time.time()+14400)
        self.store.put({'key':'saved'}, {'items':[], 'totalSum':1})
        restarted = SyncService(Store(self.path/'receipts.sqlite3'), lambda:None)
        run = restarted.journal.runs()['runs'][0]
        self.assertEqual(run['status'], 'interrupted')
        self.assertFalse(self.store.status()['running'])
        self.assertLessEqual(self.store.status()['next_sync_at'], time.time())
        self.assertTrue(self.store.has_detail('saved'))

    def test_internal_failure_keeps_safe_stack_location_without_exception_message(self):
        class Client:
            def load(self): raise RuntimeError('private-token secret-purchase')
        service = SyncService(self.store, Client)
        service.run_once()
        self.assertTrue(hasattr(service, 'journal'), 'Persistent error journal is missing')
        event = next(e for e in service.journal.events()['events'] if e['event'] == 'sync_crashed')
        self.assertEqual(event['context']['exception_type'], 'RuntimeError')
        self.assertIn('test_observability.py', event['context']['frames'])
        self.assertNotIn('private-token', json.dumps(event))
        self.assertNotIn('secret-purchase', json.dumps(event))

    def test_event_retention_prunes_only_diagnostics_and_paginates(self):
        service = SyncService(self.store, lambda:None)
        self.assertTrue(hasattr(service, 'journal'), 'Persistent event journal is missing')
        self.store.put({'key':'keep'}, {'items':[], 'totalSum':1})
        journal = service.journal
        for i in range(5):
            journal.event(None, 'request_succeeded', offset=i)
        with patch('fns_mcp.journal.EVENT_LIMIT', 3):
            journal.prune()
        first = journal.events(limit=2)
        self.assertEqual([e['context']['offset'] for e in first['events']], [4, 3])
        second = journal.events(limit=2, before_id=first['next_before_id'])
        self.assertEqual([e['context']['offset'] for e in second['events']], [2])
        self.assertTrue(self.store.has_detail('keep'))
        with self.assertRaises(ValueError):
            journal.events(limit=201)
        with self.assertRaises(ValueError):
            journal.runs(limit=0)

    def test_stale_sync_is_visible_even_when_process_is_alive(self):
        service = SyncService(self.store, lambda:None)
        self.assertTrue(callable(getattr(service, 'status', None)), 'Operational status is missing')
        self.store.state(running=True, heartbeat_at=time.time()-600, last_success_at=time.time()-40000)
        status = service.status()
        self.assertTrue(status['stalled'])
        self.assertTrue(status['stale'])
        self.assertIn('sync_stalled', status['warnings'])

    def test_five_consecutive_failures_stop_requests_and_can_resume_without_duplicates(self):
        class Client:
            broken = True
            def load(self): pass
            def receipts(self): return [{'key':str(i)} for i in range(8)]
            def detail(self, key):
                if self.broken and key != '0':
                    raise SyncError('fns_rejected_request', context={'http_status':422})
                return {'items':[], 'totalSum':1}
        client = Client()
        service = SyncService(self.store, lambda:client)
        service.run_once()
        status = service.status()
        self.assertEqual((status['last_error'], status['failed'], status['seen']),
                         ('consecutive_failures', 5, 6))
        self.assertEqual(status['pending_receipts'], 5)
        client.broken = False
        service.run_once()
        status = service.status()
        self.assertEqual((status['receipts'], status['detailed_receipts'], status['skipped']), (8, 8, 1))
        self.assertIsNone(status['last_error'])
        self.assertIsNone(status['last_error_context'])
        self.assertEqual(len(service.journal.runs()['runs']), 2)

    def test_database_write_failure_releases_lock_and_emits_safe_stdout_event(self):
        service = SyncService(self.store, lambda:None)
        with patch.object(self.store, 'connect', side_effect=sqlite3.OperationalError('private database path')), \
                self.assertLogs('fns_mcp.events', level='ERROR') as captured:
            with self.assertRaises(sqlite3.OperationalError):
                service.run_once()
        self.assertFalse(service.lock.locked())
        self.assertTrue(service.persistence_error)
        event = json.loads(captured.records[0].getMessage())
        self.assertEqual(event['event'], 'sync_crashed')
        self.assertEqual(event['context']['exception_type'], 'OperationalError')
        self.assertNotIn('private database path', json.dumps(event))


class SchedulerFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_consumes_wake_before_backoff_instead_of_busy_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'receipts.sqlite3')
            service = SyncService(store, lambda:None)
            service.wake.set()
            observed = []

            async def wait_once(awaitable, timeout):
                awaitable.close()
                observed.append((service.wake.is_set(), timeout))
                service.stop.set()
                raise asyncio.TimeoutError

            with patch.object(store, 'state', side_effect=sqlite3.OperationalError('disk full')), \
                    patch('fns_mcp.sync.asyncio.wait_for', side_effect=wait_once):
                await service.schedule()
            self.assertEqual(observed, [(False, 60)])
            self.assertTrue(service.persistence_error)
            self.assertFalse(service.scheduler_running)


if __name__ == '__main__':
    unittest.main()

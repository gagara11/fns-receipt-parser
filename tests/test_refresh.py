import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import export_nalog_receipts as upstream
from fns_mcp.client import FNSClient, SyncError, atomic_json
from fns_mcp.store import Store
from fns_mcp.sync import SyncService
from tests.test_api import FakeResponse, http_error


def jwt(exp):
    # Synthetic unsigned metadata, never a real credential.
    payload = base64.urlsafe_b64encode(json.dumps({'exp':exp}).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


class RefreshTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'session.json'
        self.saved = {'token':'old-access', 'refreshToken':'old-refresh', 'sourceDeviceId':'device'}
        atomic_json(self.path, self.saved)
        self.client = FNSClient(self.path, delay=0)
        self.sleep = patch('fns_mcp.client.time.sleep').start()
        self.now = patch('fns_mcp.client.time.time', return_value=1000).start()
        self.addCleanup(patch.stopall)

    def save(self, **fields):
        self.saved.update(fields)
        atomic_json(self.path, self.saved)

    def request(self, steps):
        # Assert the actual HTTP boundary (URL, bearer, payload) and real result.
        remaining = list(steps)
        def serve(request, timeout):
            self.assertTrue(remaining, 'Unexpected additional request')
            path, token, result = remaining.pop(0)
            self.assertEqual(request.full_url, upstream.BASE+path)
            self.assertEqual(request.get_header('Authorization'), 'Bearer '+token if token else None)
            if path == '/v1/auth/token':
                self.assertEqual(json.loads(request.data), {
                    'refreshToken':'old-refresh',
                    'deviceInfo':{'sourceDeviceId':'device', 'sourceType':'WEB', 'appVersion':'1.0.0',
                                  'metaDetails':{'userAgent':'Mozilla/5.0'}}})
            if isinstance(result, Exception):
                raise result
            return FakeResponse(result)
        with patch.object(upstream._opener, 'open', side_effect=serve):
            try:
                return self.client.request('/v1/receipt', {'offset':0})
            finally:
                self.assertEqual(remaining, [], 'Expected HTTP operation was not performed')

    def test_expired_token_code_refreshes_then_replays_original_request(self):
        result = self.request([
            ('/v1/receipt','old-access',http_error(422, b'{"code":"authentication.failed.expired.token"}')),
            ('/v1/auth/token','',{'token':'new-access','refreshToken':'new-refresh'}),
            ('/v1/receipt','new-access',{'receipts':[]})])
        self.assertEqual(result, {'receipts':[]})
        self.assertEqual(json.loads(self.path.read_text())['refreshToken'], 'new-refresh')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_jwt_expiry_refreshes_without_separate_expiry_field(self):
        self.save(token=jwt(900))
        result = self.request([
            ('/v1/auth/token','',{'token':jwt(9000), 'refreshToken':'new-refresh'}),
            ('/v1/receipt',jwt(9000),{'receipts':[]})])
        self.assertEqual(result, {'receipts':[]})

    def test_token_inside_five_minute_margin_refreshes_before_request(self):
        self.save(token=jwt(1299))
        result = self.request([
            ('/v1/auth/token','',{'token':jwt(9000)}),
            ('/v1/receipt',jwt(9000),{'receipts':[]})])
        self.assertEqual(result, {'receipts':[]})

    def test_valid_jwt_does_not_trigger_unnecessary_refresh(self):
        self.save(token=jwt(9000))
        self.assertEqual(self.request([('/v1/receipt',jwt(9000),{'receipts':[]})]), {'receipts':[]})

    def test_expiry_is_checked_again_during_long_sync(self):
        self.save(token=jwt(1400))
        self.assertEqual(self.request([('/v1/receipt',jwt(1400),{'receipts':[]})]), {'receipts':[]})
        self.now.return_value = 1200
        self.assertEqual(self.request([
            ('/v1/auth/token','',{'token':jwt(9000)}),
            ('/v1/receipt',jwt(9000),{'receipts':[]})]), {'receipts':[]})

    def test_rotated_token_without_expiry_does_not_inherit_expired_hint(self):
        self.save(tokenExpireIn='1970-01-01T00:10:00Z')
        self.assertEqual(self.request([
            ('/v1/auth/token','',{'token':jwt(9000)}),
            ('/v1/receipt',jwt(9000),{'receipts':[]})]), {'receipts':[]})
        self.assertNotIn('tokenExpireIn', json.loads(self.path.read_text()))
        self.client = FNSClient(self.path, delay=0)
        self.assertEqual(self.request([('/v1/receipt',jwt(9000),{'receipts':[]})]), {'receipts':[]})

    def test_explicit_expiry_is_used_for_opaque_access_tokens(self):
        self.save(tokenExpireIn='1970-01-01T00:10:00Z')
        self.assertEqual(self.request([
            ('/v1/auth/token','',{'token':'new-access','tokenExpireIn':'1970-01-01T03:00:00Z'}),
            ('/v1/receipt','new-access',{'receipts':[]})]), {'receipts':[]})

    def test_malformed_explicit_expiry_falls_back_to_valid_jwt(self):
        self.save(token=jwt(9000), tokenExpireIn='not-a-date')
        self.assertEqual(self.request([('/v1/receipt',jwt(9000),{'receipts':[]})]), {'receipts':[]})

    def test_malformed_jwt_metadata_does_not_crash_or_force_refresh(self):
        for token in ('opaque', 'a.!bad!.c', jwt('not-a-number'), jwt(None), jwt(float('inf')), jwt(True)):
            with self.subTest(token=token):
                self.save(token=token)
                self.client = FNSClient(self.path, delay=0)
                self.assertEqual(self.request([('/v1/receipt',token,{'receipts':[]})]), {'receipts':[]})

    def test_second_auth_rejection_does_not_loop_refresh(self):
        with self.assertRaisesRegex(SyncError, '^authentication_required$'):
            self.request([
                ('/v1/receipt','old-access',http_error(422,b'{"code":"authentication.failed.expired.token"}')),
                ('/v1/auth/token','',{'token':'new-access'}),
                ('/v1/receipt','new-access',http_error(422,b'{"code":"authentication.failed.expired.token"}'))])

    def test_proactive_refresh_also_counts_toward_one_refresh_per_request(self):
        self.save(token=jwt(900))
        with self.assertRaisesRegex(SyncError, '^authentication_required$'):
            self.request([
                ('/v1/auth/token','',{'token':'new-access'}),
                ('/v1/receipt','new-access',http_error(401))])

    def test_revoked_refresh_is_terminal_and_keeps_session_on_disk(self):
        self.save(token=jwt(900))
        with self.assertRaisesRegex(SyncError, '^authentication_required$'):
            self.request([('/v1/auth/token','',http_error(422,b'{"code":"authentication.failed"}'))])
        self.assertEqual(json.loads(self.path.read_text()), self.saved)

    def test_temporary_refresh_failure_keeps_session_for_later_retry(self):
        self.save(token=jwt(900))
        with self.assertRaises(SyncError) as caught:
            self.request([('/v1/auth/token','',http_error(503,b'{"code":"temporary.failure"}')) for _ in range(3)])
        self.assertEqual(str(caught.exception), 'refresh_temporarily_failed')
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.context['operation'], 'token_refresh')
        self.assertEqual(json.loads(self.path.read_text()), self.saved)

    def test_unknown_refresh_rejection_is_not_reported_as_temporary(self):
        self.save(token=jwt(900))
        with self.assertRaisesRegex(SyncError, '^refresh_rejected$'):
            self.request([('/v1/auth/token','',http_error(422,b'{"code":"validation.failed"}'))])
        self.assertEqual(json.loads(self.path.read_text()), self.saved)

    def test_rejected_or_invalid_refresh_stops_sync_instead_of_repeating_for_each_receipt(self):
        for code in ('refresh_rejected', 'invalid_refresh_response'):
            with self.subTest(code=code):
                class Client:
                    def load(self): pass
                    def receipts(self): return [{'key':'first'}, {'key':'second'}]
                    def detail(self, key): raise SyncError(code)
                store = Store(self.path.parent/(code+'.sqlite3'))
                service = SyncService(store, Client)
                service.run_once()
                self.assertEqual(store.status()['last_error'], code)
                self.assertEqual(store.status()['seen'], 1)
                self.assertFalse(store.get('second')['found'])


if __name__ == '__main__':
    unittest.main()

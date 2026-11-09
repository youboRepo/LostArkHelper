"""QQ 凭据前置校验：使用虚构凭据，不访问腾讯或提交奖励。"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError, HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import helper
import app
import automation


class LoginTests(unittest.TestCase):
    def setUp(self):
        helper._LOGIN_CACHE.clear()
        self.config = {'cookie': 'private-game-cookie', 'form': {
            'access_token': 'FakeAccess', 'appid': '102037747', 'openid': 'FakeOpenid'}}
        self.good = {'client_id': '102037747', 'openid': 'FakeOpenid'}
        self.factory = patch.object(helper.urllib.request, 'build_opener')
        self.opener = self.factory.start().return_value
        self.addCleanup(self.factory.stop)
        self.addCleanup(helper._LOGIN_CACHE.clear)
        self.respond(self.good)

    def respond(self, data):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.opener.open.side_effect = lambda *a, **k: io.BytesIO(raw)

    def test_valid_json_callback_and_no_game_cookie(self):
        for body in (self.good, b'callback( {"client_id":"102037747","openid":"FakeOpenid"} );'):
            with self.subTest(body=body):
                self.respond(body)
                helper.validate_login(self.config, force=True)
                request = self.opener.open.call_args.args[0]
                self.assertEqual(request.full_url, 'https://graph.qq.com/oauth2.0/me?access_token=FakeAccess&fmt=json')
                self.assertIsNone(request.get_header('Cookie'))
                self.assertEqual(request.get_method(), 'GET')
                self.assertEqual(self.opener.open.call_args.kwargs['timeout'], 15)

    def test_expired_token_stops_game_query_and_claim(self):
        self.respond({'error': 100014, 'error_description': 'access token is expired FakeAccess'})
        for endpoint in (helper.LIST, helper.CLAIM):
            with self.subTest(endpoint=endpoint), self.assertRaises(helper.ApiError) as caught:
                helper.request(self.config, endpoint)
            self.assertEqual(caught.exception.kind, 'auth_invalid')
            self.assertEqual(caught.exception.code, 100014)
            self.assertIn('更新 token', str(caught.exception))
            self.assertNotIn('FakeAccess', str(caught.exception))
        self.assertEqual(len(helper._LOGIN_CACHE), 0)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertTrue(all(c.args[0].full_url.startswith('https://graph.qq.com/') for c in self.opener.open.call_args_list))

    def test_identity_mismatch_stops(self):
        for key in ('client_id', 'openid'):
            self.respond(dict(self.good, **{key: 'other'}))
            with self.assertRaises(helper.ApiError) as caught:
                helper.validate_login(self.config)
            self.assertEqual(caught.exception.kind, 'auth_mismatch')
            self.assertFalse(helper._LOGIN_CACHE)

    def test_missing_malformed_large_or_unreachable_not_reported_as_unfinished(self):
        for body in (b'not json', b'[]', b'{}', b'x' * 16385):
            self.respond(body)
            with self.assertRaises(helper.ApiError) as caught:
                helper.request(self.config, helper.LIST)
            self.assertEqual(caught.exception.kind, 'auth_check')
        for error in (URLError('sensitive-url-FakeAccess'), TimeoutError(),
                      HTTPError('sensitive-url-FakeAccess', 302, 'redirect', {}, None)):
            self.opener.open.side_effect = error
            with self.assertRaises(helper.ApiError) as caught:
                helper.request(self.config, helper.LIST)
            self.assertEqual(caught.exception.kind, 'auth_check')
            self.assertNotIn('FakeAccess', str(caught.exception))
        self.assertFalse(helper._LOGIN_CACHE)

    def test_only_success_cached_and_force_or_changed_credentials_recheck(self):
        helper.validate_login(self.config)
        helper.validate_login(self.config)
        self.assertEqual(self.opener.open.call_count, 1)
        helper.validate_login(self.config, force=True)
        self.assertEqual(self.opener.open.call_count, 2)
        changed = dict(self.config, form=dict(self.config['form'], access_token='OtherAccess'))
        helper.validate_login(changed)
        self.assertEqual(self.opener.open.call_count, 3)
        self.respond({'error': 100014})
        with self.assertRaises(helper.ApiError): helper.validate_login(self.config, force=True)
        with self.assertRaises(helper.ApiError): helper.validate_login(self.config)
        self.assertEqual(self.opener.open.call_count, 5)
        # 缓存键为摘要，不含原始凭据。
        self.assertTrue(all(len(k) == 64 and 'Access' not in k for k in helper._LOGIN_CACHE))

    def test_cache_expires(self):
        with patch.object(helper.time, 'monotonic', return_value=100): helper.validate_login(self.config)
        with patch.object(helper.time, 'monotonic', return_value=161): helper.validate_login(self.config)
        self.assertEqual(self.opener.open.call_count, 2)

    def test_expired_status_and_execution_are_errors_not_zero_tasks(self):
        self.respond({'error': 100014})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # 不同端具有自己的凭据存储；状态测试不触碰真实存储。
            (root / 'config-dashboard.json').write_text('{}')
            with patch.object(app, 'read_config', return_value=self.config):
                status = app.status(root)
            self.assertTrue(status['needs_token_update'])
            self.assertIn('更新 token', status['error'])
            self.assertNotIn('tasks', status)
            report = {}
            with self.assertRaises(helper.ApiError): automation.run(helper, self.config, root, report=report)
            self.assertEqual(report['claimed'], [])
            self.assertFalse((root / 'pending-operation.json').exists())

    def test_incomplete_credentials_rejected_without_network(self):
        for key in ('access_token', 'appid', 'openid'):
            bad = dict(self.config, form=dict(self.config['form'], **{key: ''}))
            with self.assertRaises(helper.ApiError): helper.validate_login(bad)
        self.opener.open.assert_not_called()


if __name__ == '__main__': unittest.main()

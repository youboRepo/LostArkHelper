"""导入与待确认恢复回归；使用虚构凭据和假接口，不访问游戏服务。"""
from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import app
import auth
import automation
import helper
import import_link



def sample_link(**updates):
    values = dict(token='ExampleToken', userId='123', appOpenid='ExampleOpenid',
                  appid='102037747', roleId='456', serverId='4', areaId='50',
                  uin='123456', accType='qc', accessToken='ExampleAccess',
                  access_token='ExampleAccess', serverName='卡丹', areaName='卢佩恩', roleName='测试角色')
    values.update(updates)
    return 'https://lostark.qq.com/cp/a20260818stjb/h5.html?' + urlencode(values)


class ImportTests(unittest.TestCase):
    def test_parameter_names_case_insensitive_values_unchanged(self):
        for transform in (lambda k:k, str.lower, str.upper, str.swapcase):
            from urllib.parse import parse_qsl
            link = 'https://lostark.qq.com/cp/test/h5.html?' + urlencode([
                (transform(k), v) for k, v in parse_qsl(sample_link().split('?', 1)[1])])
            config, info = import_link.parse_link(link)
            self.assertEqual(config['form']['token'], 'ExampleToken')
            self.assertEqual(config['form']['access_token'], 'ExampleAccess')
            self.assertEqual(config['form']['login_type'], 'qc')
            self.assertEqual(info, dict(server='卡丹', area='卢佩恩', role='测试角色'))

    def test_identical_duplicates_accepted_without_changing_values(self):
        expected = import_link.parse_link(sample_link())
        for suffix in ('&acctype=qc', '&TOKEN=ExampleToken', '&ACCESSTOKEN=ExampleAccess', '&accType=qc&ACCTYPE=qc', '&token=%45xampleToken'):
            with self.subTest(suffix=suffix):
                self.assertEqual(import_link.parse_link(sample_link() + suffix), expected)

    def test_case_alias_conflicts_and_conflicting_access_rejected(self):
        for suffix in ('&acctype=wx', '&TOKEN=exampletoken', '&USERID=999', '&ACCESSTOKEN=DifferentAccess', '&acctype='):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                import_link.parse_link(sample_link() + suffix)
        for updates in (dict(accType='wx'), dict(access_token='OtherAccess'), dict(token='invalid value')):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                import_link.parse_link(sample_link(**updates))

    def test_pending_error_invites_token_update(self):
        class Fake:
            VERIFIED = helper.VERIFIED
            ApiError = helper.ApiError
            @staticmethod
            def sign_status(config): return True
            @staticmethod
            def tasks(config): return [dict(id=2, status=1)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            automation.save_state(root/'pending-operation.json', dict(action=2))
            with self.assertRaisesRegex(helper.ApiError, '更新 token'):
                automation.run(Fake, {}, root)
            self.assertTrue((root/'pending-operation.json').exists())

    def test_import_recovery_is_scoped_and_only_after_successful_save(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            other = root/'other-account'
            other.mkdir()
            other_guard = other/'pending-operation.json'
            other_guard.write_text('{"action": 7}')
            original, _ = import_link.parse_link(sample_link())
            saved = [original]
            configured = [True]
            stack.enter_context(patch.object(app, 'ROOT', root))
            login = stack.enter_context(patch.object(helper, 'validate_login', return_value=None))
            user = auth.create_user(root, 'test', 'PrivateTestPassword123', legacy=True)
            stack.enter_context(patch.object(app.Handler, 'current_user', return_value=user))
            stack.enter_context(patch.object(app, 'read_config', side_effect=lambda _: saved[0]))
            (root/'config-dashboard.json').write_text(json.dumps(original))
            save = stack.enter_context(patch.object(app, 'save_state'))
            def write(*args):
                saved[0] = args[-1]
                configured[0] = True
            save.side_effect = write
            server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            stack.enter_context(patch.object(app, 'PORT', server.server_address[1]))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            def call(body):
                c = http.client.HTTPConnection('127.0.0.1', app.PORT)
                try:
                    c.request('POST', '/api/import', json.dumps(body), {
                        'Origin': 'http://127.0.0.1:' + str(app.PORT),
                        'Content-Type': 'application/json', 'X-Ark-Request': '1'})
                    r = c.getresponse()
                    return r.status, json.loads(r.read())
                finally:
                    c.close()
            guard = root/'pending-operation.json'
            try:
                # 同一账号导入有效新凭据后清除自己的记录，不改其他账号，也不自动领奖。
                guard.write_text('{"action": 2}')
                with patch.object(helper, 'request') as request:
                    status, data = call(dict(url=sample_link(token='NewToken') + '&acctype=qc', replace=True))
                    request.assert_not_called()
                self.assertEqual(status, 200)
                self.assertTrue(data['pending_cleared'])
                self.assertIn('限制已解除', data['message'])
                self.assertFalse(guard.exists())
                self.assertTrue(other_guard.exists())
                self.assertEqual(saved[0]['form']['token'], 'NewToken')

                # 原来阻塞的任务可在新一轮查询后执行；已领取任务仍跳过。
                class Fake:
                    VERIFIED = helper.VERIFIED
                    ApiError = helper.ApiError
                    CLAIM = 'claim'
                    calls = []
                    @staticmethod
                    def sign_status(config): return True
                    @classmethod
                    def tasks(cls, config):
                        return [dict(id=2, status=2 if cls.calls else 1, name='test'), dict(id=3, status=2)]
                    @classmethod
                    def request(cls, config, endpoint, extra=None): cls.calls.append(extra)
                self.assertEqual(automation.run(Fake, saved[0], root), 0)
                self.assertEqual(Fake.calls, [dict(task_id='2', type='1')])

                # 不勾选更新、错误链接和不同账号/角色都不能解除记录。
                for body, expected in (
                    (dict(url=sample_link()), 409),
                    (dict(url='https://invalid.example/', replace=True), 400),
                    (dict(url=sample_link(userId='999'), replace=True), 409),
                    (dict(url=sample_link(roleId='999'), replace=True), 409),
                    (dict(url=sample_link(appOpenid='OtherOpenid'), replace=True), 409)):
                    guard.write_text('{"action": 2}')
                    before = save.call_count
                    self.assertEqual(call(body)[0], expected)
                    self.assertEqual(save.call_count, before)
                    self.assertTrue(guard.exists())
                # 过期或网络校验失败不能写入新凭据，也不能清除待确认记录。
                for kind, expected in (('auth_invalid', 400), ('auth_mismatch', 400), ('auth_check', 503)):
                    guard.write_text('{"action": 2}')
                    login.side_effect = helper.ApiError('请更新 token', kind)
                    before = save.call_count
                    status, data = call(dict(url=sample_link(), replace=True))
                    self.assertEqual(status, expected)
                    self.assertIn('更新 token', data['error'])
                    self.assertEqual(save.call_count, before)
                    self.assertTrue(guard.exists())
                login.side_effect = None
                # 磁盘/凭据保存失败时保留保护。
                save.side_effect = OSError('simulated write failure')
                self.assertEqual(call(dict(url=sample_link(), replace=True))[0], 500)
                self.assertTrue(guard.exists())
                save.side_effect = write
                # 即使账号还没有凭据，首次导入也不越过校验；成功保存可恢复遗留记录。
                configured[0] = False
                (root/'config-dashboard.json').unlink()
                self.assertEqual(call(dict(url=sample_link()))[0], 200)
                self.assertFalse(guard.exists())
                # 没有待确认记录的正常更新也成功。
                self.assertFalse(call(dict(url=sample_link(), replace=True))[1]['pending_cleared'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()

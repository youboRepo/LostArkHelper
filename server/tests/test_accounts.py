"""多账号：绑定管理、独立待确认记录、独立运行记录与 HTTP 路由；使用假接口，不连接游戏服务器。"""
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import accounts
import automation
import helper
import store

USER_COUNT = 0


def config(user_id, role='R1'):
    form = {k: 'x' for k in ('token', 'openid', 'access_token', 'inv_user_id', 'login_type', 'iUin',
                             'sGiftPartition', 'sGiftArea', 'acctype', 'appid')}
    form.update(user_id=str(user_id), sRoleld=role)
    return {'form': form, 'cookie': 'c'}


class MultiAPI:
    """每个游戏账号（按 form.user_id）有独立的签到/任务状态。"""
    SIGN_IN = 'sign'
    CLAIM = 'claim'
    VERIFIED = helper.VERIFIED
    ApiError = helper.ApiError

    def __init__(self, broken=()):
        self.state = {}
        self.broken = set(broken)
        self.calls = []
        self.lock = threading.Lock()

    def _s(self, cfg):
        with self.lock:
            return self.state.setdefault(cfg['form']['user_id'], {'signed': False, 'tasks': {6: 0}})

    def sign_status(self, cfg):
        return self._s(cfg)['signed']

    def tasks(self, cfg):
        return [{'id': i, 'status': s, 'type': 3, 'name': str(i)} for i, s in self._s(cfg)['tasks'].items()]

    def request(self, cfg, endpoint, extra=None):
        uid = cfg['form']['user_id']
        self.calls.append((uid, endpoint))
        if uid in self.broken:
            raise helper.ApiError('模拟超时', 'timeout')
        st = self._s(cfg)
        if endpoint == self.SIGN_IN:
            st['signed'] = True
            st['tasks'][6] = 1
        else:
            st['tasks'][int(extra['task_id'])] = 2


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def add(self, uid, role='R1', label=None):
        aid = accounts.next_id(self.root)
        accounts.save(self.root, aid, config(uid, role), label or '账号 ' + aid)
        return aid

    def test_first_account_uses_legacy_location(self):
        self.assertEqual(self.add(1), '1')
        self.assertTrue((self.root / 'config-dashboard.json').is_file())
        self.assertEqual(self.add(2), '2')
        self.assertTrue((self.root / 'accounts' / '2' / 'config-dashboard.json').is_file())
        self.assertEqual([a['id'] for a in accounts.list_accounts(self.root)], ['1', '2'])

    def test_ids_are_not_reused_and_max_is_enforced(self):
        self.add(1)
        self.add(2)
        third = self.add(3)
        accounts.remove(self.root, third)
        self.assertEqual(self.add(4), '4')
        accounts.remove(self.root, '1')
        self.assertEqual(self.add(5), '5')  # 账号 1 的编号同样不复用
        self.assertIsNone(accounts.get(self.root, '1'))
        for n in range(6, 6 + accounts.MAX_ACCOUNTS):
            if len(accounts.list_accounts(self.root)) >= accounts.MAX_ACCOUNTS:
                break
            self.add(n)
        with self.assertRaises(ValueError):
            accounts.next_id(self.root)

    def test_invalid_ids_cannot_escape_the_user_directory(self):
        for bad in ('0', '../x', '1/../../x', '01', '', None, 2, '1000'):
            self.assertFalse(accounts.valid_id(bad))
            self.assertIsNone(accounts.get(self.root, bad))
        with self.assertRaises(ValueError):
            accounts.account_root(self.root, '../evil')

    def test_duplicate_role_is_detected(self):
        self.add(1, 'A')
        self.assertIsNotNone(accounts.find_duplicate(self.root, config(1, 'A')))
        self.assertIsNone(accounts.find_duplicate(self.root, config(1, 'B')))
        self.assertIsNone(accounts.find_duplicate(self.root, config(2, 'A')))

    def test_fresh_user_starts_at_one_and_legacy_without_counter_continues(self):
        self.assertEqual(self.add(1), '1')
        (self.root / 'accounts' / '.next').unlink()  # 旧部署：没有计数器
        self.assertEqual(self.add(2), '2')

    def test_legacy_first_remove_reserves_id_before_deletion(self):
        accounts.save(self.root, '1', config(1), 'legacy')
        self.assertFalse((self.root / 'accounts' / '.next').exists())
        accounts.remove(self.root, '1')
        self.assertEqual(accounts.next_id(self.root), '2')

    def test_history_is_a_lower_bound_when_counter_missing(self):
        store.begin(self.root, None, '7')
        self.assertEqual(accounts.next_id(self.root), '8')

    def test_corrupt_counter_fails_closed(self):
        counter = self.root / 'accounts' / '.next'
        counter.parent.mkdir()
        counter.write_text('broken')
        with self.assertRaises(ValueError):
            accounts.next_id(self.root)

    def test_quarantine_retires_account_and_preserves_pending_guard(self):
        self.add(1)
        aid = self.add(2, 'B', 'duplicate')
        root = accounts.account_root(self.root, aid)
        guard = root / 'pending-operation.json'
        guard.write_text('{"action": 6}')
        before = guard.read_bytes()
        dest = accounts.quarantine_pending(self.root, aid)
        self.assertFalse(root.exists())
        self.assertEqual((dest / 'pending-operation.json').read_bytes(), before)
        self.assertIsNone(accounts.get(self.root, aid))
        duplicate = accounts.find_duplicate(self.root, config(2, 'B'))
        self.assertTrue(duplicate['quarantined'])
        self.assertEqual(accounts.next_id(self.root), '3')
        with self.assertRaises(ValueError):
            accounts.quarantine_pending(self.root, '1')

    def test_remove_is_refused_while_operation_pending(self):
        self.add(1)
        two = self.add(2)
        for aid in ('1', two):
            guard = accounts.account_root(self.root, aid) / 'pending-operation.json'
            guard.write_text('{"action": 6}')
            with self.assertRaises(accounts.PendingOperation):
                accounts.remove(self.root, aid)
            self.assertTrue(guard.exists())
            self.assertIsNotNone(accounts.get(self.root, aid))
            guard.unlink()
            accounts.remove(self.root, aid)

    def test_remove_deletes_credentials_but_not_others(self):
        self.add(1)
        two = self.add(2)
        accounts.remove(self.root, two)
        self.assertFalse((self.root / 'accounts' / two).exists())
        self.assertIsNotNone(accounts.get(self.root, '1'))
        accounts.remove(self.root, '1')
        self.assertEqual(accounts.list_accounts(self.root), [])
        self.assertFalse((self.root / 'config-dashboard.json').exists())

    def test_old_database_gains_account_column(self):
        db = sqlite3.connect(str(self.root / 'automation.sqlite3'))
        db.executescript('''CREATE TABLE runs (id INTEGER PRIMARY KEY, slot TEXT UNIQUE, started TEXT NOT NULL,
          finished TEXT, outcome TEXT NOT NULL, details TEXT NOT NULL);
          INSERT INTO runs(slot,started,outcome,details) VALUES ('s','t','success','{}');''')
        db.commit()
        db.close()
        self.assertEqual(store.history(self.root)['items'][0]['account'], '1')

    def test_slots_are_per_account_and_interrupts_are_isolated(self):
        a = store.begin(self.root, '2026-01-01T09:00', '1')
        b = store.begin(self.root, '2026-01-01T09:00', '2')
        self.assertTrue(a and b and a != b)
        self.assertIsNone(store.begin(self.root, '2026-01-01T09:00', '2'))
        store.begin(self.root, None, '2')  # 账号 2 的新运行只中断账号 2 之前未结束的记录
        outcomes = {r['id']: r['outcome'] for r in store.history(self.root)['items']}
        self.assertEqual(outcomes[a], 'running')
        self.assertEqual(outcomes[b], 'interrupted')
        self.assertEqual([r['id'] for r in store.history(self.root, None, '1')['items']], [a])


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for n in (1, 2, 3):
            aid = accounts.next_id(self.root)
            accounts.save(self.root, aid, config(n, 'R%d' % n), '角色%d' % n)

    def run_all(self, api):
        return [automation.execute_user(self.root, api, False, True, a) for a in accounts.list_accounts(self.root)]

    def test_every_account_signs_in_independently(self):
        api = MultiAPI()
        self.assertEqual(self.run_all(api), [0, 0, 0])
        self.assertEqual(sorted(c for c in api.calls if c[1] == 'sign'), [('1', 'sign'), ('2', 'sign'), ('3', 'sign')])
        runs = store.history(self.root)['items']
        self.assertEqual(sorted(r['account'] for r in runs), ['1', '2', '3'])
        self.assertTrue(all(r['outcome'] == 'success' for r in runs))
        self.assertEqual({r['details']['account']['label'] for r in runs}, {'角色1', '角色2', '角色3'})

    def test_one_failing_account_does_not_block_or_poison_others(self):
        api = MultiAPI(broken={'2'})
        self.assertEqual(self.run_all(api), [0, 1, 0])
        self.assertTrue((self.root / 'accounts' / '2' / 'pending-operation.json').exists())
        self.assertFalse((self.root / 'pending-operation.json').exists())
        self.assertFalse((self.root / 'accounts' / '3' / 'pending-operation.json').exists())
        by_account = {r['account']: r['outcome'] for r in store.history(self.root)['items']}
        self.assertEqual(by_account, {'1': 'success', '2': 'error', '3': 'success'})
        # 账号 2 的待确认记录仍阻止它自己再次提交，但不影响其他账号。
        calls_before = len(api.calls)
        api.broken.clear()
        self.assertEqual(self.run_all(api), [0, 1, 0])
        self.assertNotIn('2', [c[0] for c in api.calls[calls_before:] if c[1] in ('sign', 'claim')])

    def test_manual_run_reports_per_account_and_locks_only_that_account(self):
        api = MultiAPI()
        two = accounts.get(self.root, '2')
        held = automation.OperationLock(two['root'])
        self.assertTrue(held.acquire())
        try:
            self.assertEqual(automation.manual_run(self.root, two, api)[0], 409)
            code, message = automation.manual_run(self.root, accounts.get(self.root, '3'), api)
            self.assertEqual(code, 200, message)
        finally:
            held.release()
        self.assertEqual(automation.manual_run(self.root, two, api)[0], 200)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.app = app
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        app.ROOT = Path(cls.tmp.name)
        app.helper = MultiAPI()
        cls.api = app.helper
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        app.PORT = cls.server.server_address[1]
        cls.port = app.PORT
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        import auth
        auth.create_user(app.ROOT, 'tester', 'correct horse battery')
        cls.cookie = None
        status, body, cookie = cls.call('POST', '/api/auth/login', {'username': 'tester', 'password': 'correct horse battery'})
        assert status == 200, body
        cls.cookie = cookie.split(';')[0]

    @classmethod
    def call(cls, method, path, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', cls.port)
        headers = {'Host': '127.0.0.1:%d' % cls.port}
        data = None
        if method == 'POST':
            data = json.dumps(body or {})
            headers.update({'Content-Type': 'application/json', 'X-Ark-Request': '1',
                            'Origin': 'http://127.0.0.1:%d' % cls.port})
        if cls.cookie:
            headers['Cookie'] = cls.cookie
        conn.request(method, path, data, headers)
        res = conn.getresponse()
        payload = json.loads(res.read())
        cookie = res.getheader('Set-Cookie')
        conn.close()
        return res.status, payload, cookie

    @staticmethod
    def link(uid, role):
        # roleId 必须是数字；展示名取自 roleName。
        return ('https://lostark.qq.com/a?token=tk&userId={0}&appOpenid=oid{0}&appid=102037747&roleId={2}'
                '&serverId=2&areaId=3&uin=99&acctype=qc&accessToken=at&roleName={1}').format(
                    uid, role, sum(map(ord, role)))

    def test_full_flow(self):
        call = self.call
        self.assertEqual(call('GET', '/api/accounts')[1]['accounts'], [])
        self.assertEqual(call('GET', '/api/status')[1], {'configured': False, 'source': None})
        self.assertEqual(call('POST', '/api/run')[0], 400)

        # 旧客户端：不带 account 的导入创建账号 1
        status, body, _ = call('POST', '/api/import', {'url': self.link(1, 'Alpha')})
        self.assertEqual(status, 200, body)
        status, body, _ = call('POST', '/api/accounts/add', {'url': self.link(2, 'Beta'), 'label': '小号'})
        self.assertEqual((status, body['account']), (201, '2'), body)
        self.assertEqual(call('POST', '/api/accounts/add', {'url': self.link(2, 'Beta')})[0], 409)  # 重复角色
        listing = call('GET', '/api/accounts')[1]['accounts']
        self.assertEqual([(a['id'], a['label']) for a in listing], [('1', 'Alpha'), ('2', '小号')])
        self.assertNotIn('cookie', json.dumps(listing))
        self.assertNotIn('tk', json.dumps(listing))

        st = call('GET', '/api/status?account=2')[1]
        self.assertEqual((st['account'], st['source'], st['signed']), ('2', '小号', False))
        self.assertEqual(call('GET', '/api/status?account=9')[0], 404)
        self.assertEqual(call('GET', '/api/status?account=..%2F..')[0], 404)

        # 只执行一个账号
        status, body, _ = call('POST', '/api/sign', {'account': '2'})
        self.assertEqual(status, 200, body)
        self.assertEqual([c for c in self.api.calls if c[1] == 'sign'], [('2', 'sign')])
        # 全部账号
        status, body, _ = call('POST', '/api/run', {'account': 'all'})
        self.assertEqual(status, 200, body)
        self.assertIn('Alpha：', body['message'])
        self.assertEqual(sorted(c[0] for c in self.api.calls if c[1] == 'sign'), ['1', '2'])
        history = call('GET', '/api/history?account=2')[1]['items']
        self.assertTrue(history and all(r['account'] == '2' for r in history))

        # 更新 token 须确认且同一角色；不能借更新换成别的游戏账号
        self.assertEqual(call('POST', '/api/import', {'url': self.link(2, 'Beta'), 'account': '2'})[0], 409)
        self.assertEqual(call('POST', '/api/import', {'url': self.link(2, 'Beta'), 'account': '2', 'replace': True})[0], 200)
        self.assertEqual(call('POST', '/api/import', {'url': self.link(7, 'Zed'), 'account': '2', 'replace': True})[0], 409)
        self.assertEqual(call('POST', '/api/import', {'url': self.link(7, 'Zed'), 'account': '8'})[0], 404)

        self.assertEqual(call('POST', '/api/accounts/rename', {'account': '2', 'label': '新名字'})[0], 200)
        guard = next((Path(self.app.ROOT) / 'users').iterdir()) / 'accounts' / '2' / 'pending-operation.json'
        guard.write_text('{"action": 6}')
        self.assertEqual(call('POST', '/api/accounts/remove', {'account': '2'})[0], 409)
        self.assertEqual(len(call('GET', '/api/accounts')[1]['accounts']), 2)
        guard.unlink()
        self.assertEqual(call('POST', '/api/accounts/remove', {'account': '2'})[0], 200)
        self.assertEqual([a['id'] for a in call('GET', '/api/accounts')[1]['accounts']], ['1'])
        self.assertEqual(call('POST', '/api/accounts/remove', {'account': '2'})[0], 404)
        # 历史保留
        self.assertTrue(call('GET', '/api/history?account=2')[1]['items'])
        self.assertEqual(call('POST', '/api/accounts/remove', {'account': '1'})[0], 200)
        self.assertEqual(call('POST', '/api/import', {'url': self.link(8, 'Gamma')})[0], 409)
        status, body, _ = call('POST', '/api/accounts/add', {'url': self.link(8, 'Gamma')})
        self.assertEqual((status, body['account']), (201, '3'))
        user_root = next((Path(self.app.ROOT) / 'users').iterdir())
        (accounts.account_root(user_root, '3') / 'pending-operation.json').write_text('{"action": 6}')
        accounts.quarantine_pending(user_root, '3')
        code, body, _ = call('POST', '/api/accounts/add', {'url': self.link(8, 'Gamma')})
        self.assertEqual(code, 409)
        self.assertIn('隔离', body['error'])


if __name__ == '__main__':
    unittest.main()

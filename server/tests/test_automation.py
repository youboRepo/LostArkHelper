"""使用内存假接口检查提交边界；不连接游戏服务器。"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import automation
import helper
import store


class FakeAPI:
    SIGN_IN = 'sign'
    CLAIM = 'claim'
    VERIFIED = helper.VERIFIED
    ApiError = helper.ApiError

    def __init__(self, items, signed=True, uncertain=False):
        self.items = items
        self.signed = signed
        self.uncertain = uncertain
        self.calls = []

    def sign_status(self, config):
        return self.signed

    def tasks(self, config):
        return [dict(t) for t in self.items]

    def request(self, config, endpoint, extra=None):
        self.calls.append((endpoint, extra))
        if self.uncertain:
            raise helper.ApiError('模拟超时', 'timeout')
        if endpoint == self.SIGN_IN:
            self.signed = True
            for t in self.items:
                if t['id'] == 7:
                    t['status'] = 1
        else:
            for t in self.items:
                if t['id'] == int(extra['task_id']):
                    t['status'] = 2


def task(tid, status):
    return {'id': tid, 'status': status, 'type': 3, 'name': str(tid)}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_supported_and_inferred_claims_use_type_one(self):
        self.assertEqual(set(helper.VERIFIED), set(range(1, 14)))
        self.assertEqual(helper.INFERRED, set(range(6, 12)))
        api = FakeAPI([task(i, 1) for i in range(1, 14)])
        report = {}
        self.assertEqual(automation.run(api, {}, self.root, report=report), 0)
        self.assertEqual([c[1] for c in api.calls],
                         [{'task_id': str(i), 'type': '1'} for i in range(1, 14)])
        self.assertEqual(len(report['claimed']), 13)

    def test_incomplete_claimed_unknown_and_duplicate_skipped(self):
        api = FakeAPI([task(6, 0), task(7, 2), task(99, 1), task(8, 1), task(8, 1)])
        self.assertEqual(automation.run(api, {}, self.root), 0)
        self.assertFalse(api.calls)

    def test_check_only_never_submits(self):
        api = FakeAPI([task(6, 1)], signed=False)
        self.assertEqual(automation.run(api, {}, self.root, check_only=True), 0)
        self.assertFalse(api.calls)

    def test_uncertain_claim_stops_and_blocks_next_run(self):
        api = FakeAPI([task(6, 1), task(7, 1)], uncertain=True)
        report = {}
        self.assertEqual(automation.run(api, {}, self.root, report=report), 1)
        self.assertIn('更新 token', report['message'])
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(json.loads((self.root/'pending-operation.json').read_text())['action'], 6)
        with self.assertRaises(helper.ApiError):
            automation.run(api, {}, self.root)
        self.assertEqual(len(api.calls), 1)

    def test_uncertain_sign_prevents_all_rewards(self):
        api = FakeAPI([task(6, 1)], signed=False, uncertain=True)
        self.assertEqual(automation.run(api, {}, self.root), 1)
        self.assertEqual(api.calls, [('sign', None)])

    def test_recovered_confirmation_clears_pending(self):
        api = FakeAPI([task(6, 2)])
        automation.save_state(self.root/'pending-operation.json', {'action': 6})
        self.assertEqual(automation.run(api, {}, self.root), 0)
        self.assertFalse((self.root/'pending-operation.json').exists())
        self.assertFalse(api.calls)

    def test_new_sign_unlocks_milestone_in_same_run(self):
        api = FakeAPI([task(7, 0)], signed=False)
        self.assertEqual(automation.run(api, {}, self.root), 0)
        self.assertEqual(api.calls, [('sign', None), ('claim', {'task_id': '7', 'type': '1'})])

    def test_database_commits_rolls_back_and_closes(self):
        with store.connect(self.root) as db:
            db.execute("INSERT INTO settings VALUES (1, '{}')")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        with self.assertRaises(RuntimeError):
            with store.connect(self.root) as failed:
                failed.execute("UPDATE settings SET value='bad'")
                raise RuntimeError()
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute('SELECT 1')
        with store.connect(self.root) as db:
            self.assertEqual(db.execute('SELECT value FROM settings').fetchone()[0], '{}')

    def test_cross_instance_lock(self):
        a, b = automation.OperationLock(self.root), automation.OperationLock(self.root)
        self.assertTrue(a.acquire())
        try:
            self.assertFalse(b.acquire())
        finally:
            a.release()
        self.assertTrue(b.acquire())
        b.release()


if __name__ == '__main__':
    unittest.main()

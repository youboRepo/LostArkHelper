"""Offline regressions for due slots, schema migration, account removal and shared capacity."""
import concurrent.futures as cf
from contextlib import closing
import gc
import inspect
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import accounts
import app
import auth
import automation
import helper
import store


def fixture(root, count):
    root.mkdir(parents=True, exist_ok=True)
    for number in range(1, count + 1):
        path = accounts.account_root(root, str(number))
        path.mkdir(parents=True, exist_ok=True)
        (path / accounts.CONFIG).write_text(json.dumps({
            'cookie': 'synthetic', 'form': {'user_id': str(number), 'sRoleld': str(number)}}))
    return accounts.list_accounts(root)


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gc.collect)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'users' / '1'
        self.root.mkdir(parents=True)

    def test_schema_check_is_inside_serialized_transaction(self):
        with closing(sqlite3.connect(self.root / 'automation.sqlite3')) as db:
            db.execute("""CREATE TABLE runs (id INTEGER PRIMARY KEY, slot TEXT UNIQUE,
                started TEXT NOT NULL, finished TEXT, outcome TEXT NOT NULL, details TEXT NOT NULL)""")
            db.execute("INSERT INTO runs VALUES (1,'old','t',NULL,'success','{}')")
            db.commit()
        states = []
        original = sqlite3.connect
        class Connection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == 'PRAGMA table_info(runs)':
                    states.append(self.in_transaction)
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            return original(*args, **kwargs, factory=Connection)
        gate = threading.Barrier(8)
        def worker(_):
            gate.wait(timeout=5)
            try:
                store.settings(self.root)
            except Exception as exc:
                return str(exc)
            return None
        with patch.object(store.sqlite3, 'connect', connect):
            with cf.ThreadPoolExecutor(max_workers=8) as pool:
                errors = [value for value in pool.map(worker, range(8)) if value]
        self.assertEqual(errors, [])
        self.assertEqual(states, [True] * 8)
        self.assertEqual(store.history(self.root)['items'][0]['account'], '1')

    def test_connection_closes_when_migration_fails(self):
        original = sqlite3.connect
        connections = []
        class Connection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == 'PRAGMA table_info(runs)':
                    raise sqlite3.OperationalError('synthetic schema failure')
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            db = original(*args, **kwargs, factory=Connection)
            connections.append(db)
            return db
        try:
            with patch.object(store.sqlite3, 'connect', connect):
                with self.assertRaises(sqlite3.OperationalError):
                    store.settings(self.root)
            with self.assertRaises(sqlite3.ProgrammingError):
                connections[0].execute('SELECT 1')
        finally:
            for db in connections:
                db.close()

    def test_due_jobs_survive_queue_crossing_minute_and_deduplicate(self):
        targets = fixture(self.root, 10)
        due = datetime(2026, 11, 9, 9, 0, tzinfo=store.TZ)
        clock = [due]
        calls = []
        def advance():
            clock[0] += timedelta(minutes=1)
        gate = threading.Barrier(4, action=advance)
        def signed(config):
            calls.append(config['form']['user_id'])
            if int(config['form']['user_id']) <= 4:
                gate.wait(timeout=5)
            return True
        store.settings(self.root, {'enabled': True, 'mode': 'daily', 'minute': 0, 'times': ['09:00']})
        old_umask = os.umask(0o077)
        try:
            with patch.dict(os.environ, {'ARK_HELPER_DIR': str(self.base)}), patch.object(sys, 'argv', ['automation.py']), patch.object(auth, 'initialize'), patch.object(auth, 'all_users', return_value=[{}]), patch.object(auth, 'user_root', return_value=self.root), patch.object(store, 'now', lambda: clock[0]), patch.object(helper, 'sign_status', signed), patch.object(helper, 'tasks', return_value=[]):
                self.assertEqual(automation.main(), 0)
                self.assertEqual(len(calls), 10)
                # The fixed original slot, not 09:01, is used for all ten accounts.
                rows = store.history(self.root)['items']
                self.assertEqual(len(rows), 10)
                self.assertTrue(all(row['slot'].startswith('2026-11-09T09:00') for row in rows))
                if 'due_at' in inspect.signature(automation.execute_user).parameters:
                    for acct in targets:
                        self.assertEqual(automation.execute_user(self.root, helper, acct=acct, due_at=due, capacity_root=self.base), 0)
                    self.assertEqual(len(calls), 10)
        finally:
            os.umask(old_umask)

    def test_removed_queued_account_does_not_escape_or_block_another(self):
        targets = fixture(self.root, 2)
        accounts.remove(self.root, '2')
        with patch.object(helper, 'sign_status', return_value=True) as signed, patch.object(helper, 'tasks', return_value=[]):
            self.assertEqual(automation.manual_run(self.root, targets[1], helper)[0], 409)
            self.assertEqual(automation.execute_user(self.root, helper, run_now=True, acct=targets[1]), 0)
            signed.assert_not_called()
            self.assertEqual(automation.manual_run(self.root, targets[0], helper)[0], 200)
            self.assertEqual(signed.call_count, 1)

    def post_all(self, root):
        handler = object.__new__(app.Handler)
        body = b'{"account":"all"}'
        handler.path = '/api/run'
        handler.headers = {'Origin': 'http://local', 'Host': 'local', 'X-Ark-Request': '1',
                           'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.valid_host = lambda: True
        handler.current_user = lambda: root
        responses = []
        handler.reply = lambda *args: responses.append(args)
        handler.post()
        return responses[0]

    def test_mixed_batch_reports_removed_account_and_success(self):
        targets = fixture(self.root, 2)
        accounts.remove(self.root, '2')
        with patch.object(app, 'ROOT', self.base), patch.object(app, 'helper', helper), patch.object(auth, 'user_root', return_value=self.root), patch.object(accounts, 'list_accounts', return_value=targets), patch.object(helper, 'sign_status', return_value=True), patch.object(helper, 'tasks', return_value=[]):
            code, result = self.post_all(self.root)
        self.assertEqual(code, 409)
        self.assertIn('执行成功', result['error'])
        self.assertIn('已移除', result['error'])

    def test_web_requests_and_scheduler_share_four_slots(self):
        roots = [self.root, self.base / 'users' / '2']
        for root in roots:
            fixture(root, 4)
            store.settings(root)
        scheduled_root = self.base / 'users' / '3'
        acct = fixture(scheduled_root, 1)[0]
        store.settings(scheduled_root)
        release, started = threading.Event(), threading.Event()
        mutex = threading.Lock()
        active = peak = calls = 0
        def signed(config):
            nonlocal active, peak, calls
            with mutex:
                active += 1
                calls += 1
                peak = max(peak, active)
                if active == 4:
                    started.set()
            try:
                if not release.wait(timeout=10):
                    raise RuntimeError('synthetic test timed out')
                return True
            finally:
                with mutex:
                    active -= 1
        def scheduled():
            kwargs = {'capacity_root': self.base} if 'capacity_root' in inspect.signature(automation.execute_user).parameters else {}
            return automation.execute_user(scheduled_root, helper, run_now=True, acct=acct, **kwargs)
        with patch.object(app, 'ROOT', self.base), patch.object(app, 'helper', helper), patch.object(auth, 'user_root', lambda root, user: user), patch.object(helper, 'sign_status', signed), patch.object(helper, 'tasks', return_value=[]):
            with cf.ThreadPoolExecutor(max_workers=3) as pool:
                requests = [pool.submit(self.post_all, root) for root in roots]
                job = pool.submit(scheduled)
                try:
                    self.assertTrue(started.wait(timeout=5))
                    time.sleep(0.15)
                    with mutex:
                        observed = peak
                finally:
                    release.set()
                self.assertEqual([f.result()[0] for f in requests], [200, 200])
                self.assertEqual(job.result(), 0)
        self.assertLessEqual(observed, 4)
        self.assertLessEqual(peak, 4)
        self.assertEqual(calls, 9)

    @unittest.skipUnless(os.name == 'posix', 'requires native Linux flock, not the Windows adapter')
    def test_capacity_is_shared_across_processes_and_released_on_exit(self):
        service = str(Path(automation.__file__).resolve().parent)
        code = ("import sys; sys.path.insert(0,sys.argv[1]); "
                "from automation import execution_capacity; "
                "\nwith execution_capacity(sys.argv[2]):\n print('HELD',flush=True); sys.stdin.read(1)")
        children = []
        try:
            for _ in range(4):
                child = subprocess.Popen([sys.executable, '-c', code, service, str(self.base)],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(child)
            # Bound waits: a broken child must not hang the whole Linux suite.
            import select
            for child in children:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0], 'child did not acquire a slot')
                self.assertEqual(child.stdout.readline().strip(), 'HELD')
            for number in range(4):
                lock = automation.OperationLock(self.base, 'execution-{}.lock'.format(number))
                acquired = lock.acquire()
                if acquired:
                    lock.release()
                self.assertFalse(acquired)
            children[0].kill()
            children[0].wait(timeout=5)
            acquired = []
            for number in range(4):
                lock = automation.OperationLock(self.base, 'execution-{}.lock'.format(number))
                if lock.acquire():
                    acquired.append(number)
                    lock.release()
            self.assertEqual(len(acquired), 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()

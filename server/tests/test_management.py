"""本地多用户功能回归：真实 HTTP/SQLite，SMTP 和游戏接口均使用假服务。"""
from contextlib import ExitStack, closing
import http.client
import json
import os
from pathlib import Path
import smtplib
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'service'))
import admin
import app
import auth
import automation
import helper
import notifications
import store
from test_import import sample_link


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base/'panel.key').write_text('PrivateOwnerPassword123')
        auth.initialize(self.base)
        self.owner = auth.authenticate(self.base, 'ark', 'PrivateOwnerPassword123')
        self.user = auth.create_user(self.base, 'tester', 'PrivateUserPassword123')
        self.root = auth.user_root(self.base, self.user)
        self.current = [self.user]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(app, 'ROOT', self.base))
        self.stack.enter_context(patch.object(app.Handler, 'current_user', side_effect=lambda: self.current[0]))
        self.login = self.stack.enter_context(patch.object(helper, 'validate_login', return_value=None))
        self.smtp = self.stack.enter_context(patch.object(notifications.smtplib, 'SMTP_SSL'))
        self.smtp.return_value.__enter__.return_value.send_message.return_value = {}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        self.stack.enter_context(patch.object(app, 'PORT', self.server.server_address[1]))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1])
        try:
            connection.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), {
                'Origin': 'http://127.0.0.1:' + str(app.PORT), 'Content-Type': 'application/json', 'X-Ark-Request': '1'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally: connection.close()

    def configure_mail(self):
        notifications.save_mail(self.base, dict(enabled=True, sender='sender@qq.com', authorization_code='abcdefghijklmnop'))
        auth.notification_preferences(self.base, self.user, dict(email='recipient@example.com', enabled=True))

    def test_admin_routes_require_auth_and_role_for_all_methods(self):
        for user, expected in ((None, 401), (self.user, 403)):
            self.current[0] = user
            for path, body in (('/api/admin/users', None), ('/api/admin/mail', None),
                               ('/api/admin/mail', dict(enabled=True, sender='x@qq.com', authorization_code='abcdefghijklmnop'))):
                self.assertEqual(self.request(path, body)[0], expected)
        self.assertFalse((self.base/notifications.MAIL_FILE).exists())
        self.current[0] = self.owner
        status, data = self.request('/api/admin/users')
        self.assertEqual(status, 200)
        self.assertEqual(data['total'], 2)
        self.assertNotIn('password', json.dumps(data))
        self.assertNotIn('cookie', json.dumps(data))
        self.assertEqual(self.request('/api/admin/users?offset=-1')[0], 400)
        self.assertEqual(self.request('/api/admin/users?limit=101')[0], 400)

    def test_role_is_refreshed_from_database_and_normal_signup_cannot_elevate(self):
        token = auth.new_session(self.base, self.user)
        self.assertFalse(auth.session_user(self.base, token)['is_admin'])
        with closing(auth.connect(self.base)) as db, db:
            db.execute('UPDATE users SET is_admin=1 WHERE id=?', (self.user['id'],))
        self.assertTrue(auth.session_user(self.base, token)['is_admin'])
        self.assertTrue(auth.public_user(self.owner)['is_admin'])
        status, _ = self.request('/api/auth/register', dict(username='ordinary', password='PrivateUserPassword123', is_admin=True))
        self.assertEqual(status, 201)
        self.assertFalse(auth.authenticate(self.base, 'ordinary', 'PrivateUserPassword123')['is_admin'])

    def test_first_import_enables_random_hourly_once_and_update_preserves_plan(self):
        with patch.object(store.secrets, 'randbelow', return_value=59) as random:
            status, data = self.request('/api/import', dict(url=sample_link()))
            self.assertEqual(status, 200)
            self.assertTrue(data['schedule_initialized'])
            self.assertEqual(store.settings(self.root), dict(store.DEFAULT, enabled=True, minute=59))
            random.assert_called_once_with(60)
            custom = dict(store.DEFAULT, enabled=False, mode='daily', times=['17:35'])
            store.settings(self.root, custom)
            self.assertEqual(self.request('/api/import', dict(url=sample_link(token='NewToken'), replace=True))[0], 200)
            self.assertEqual(store.settings(self.root), custom)
            self.assertEqual(random.call_count, 1)

    def test_random_zero_and_persistent_marker_survive_configuration_recreation(self):
        with patch.object(store.secrets, 'randbelow', return_value=0):
            self.assertEqual(self.request('/api/import', dict(url=sample_link()))[0], 200)
        self.assertEqual(store.settings(self.root)['minute'], 0)
        custom = dict(store.DEFAULT, minute=28)
        store.settings(self.root, custom)
        (self.root/'config-dashboard.json').unlink()
        with patch.object(store.secrets, 'randbelow') as random:
            self.request('/api/import', dict(url=sample_link()))
            random.assert_not_called()
        self.assertEqual(store.settings(self.root), custom)

    def test_existing_account_updates_do_not_auto_enable_or_randomize(self):
        (self.root/'config-dashboard.json').write_text(json.dumps(app.importer.parse_link(sample_link())[0]))
        before = dict(store.DEFAULT, mode='daily', times=['11:25'])
        store.settings(self.root, before)
        with patch.object(store.secrets, 'randbelow') as random:
            self.assertEqual(self.request('/api/import', dict(url=sample_link(), replace=True))[0], 200)
            random.assert_not_called()
        self.assertEqual(store.settings(self.root), before)

    def test_failed_validation_or_config_save_cannot_enable_schedule(self):
        self.login.side_effect = helper.ApiError('请更新 token', 'auth_invalid')
        self.assertEqual(self.request('/api/import', dict(url=sample_link()))[0], 400)
        self.assertFalse(store.settings(self.root)['enabled'])
        self.assertFalse((self.root/'config-dashboard.json').exists())
        self.login.side_effect = None
        with patch.object(app, 'save_state', side_effect=OSError()):
            self.assertEqual(self.request('/api/import', dict(url=sample_link()))[0], 500)
        self.assertFalse(store.settings(self.root)['enabled'])

    def test_first_schedule_save_failure_rolls_back_new_configuration(self):
        with patch.object(store, 'token_imported', side_effect=sqlite3.OperationalError()):
            self.assertEqual(self.request('/api/import', dict(url=sample_link()))[0], 500)
        self.assertFalse((self.root/'config-dashboard.json').exists())
        self.assertFalse(store.settings(self.root)['enabled'])
        self.assertEqual(self.request('/api/import', dict(url=sample_link()))[0], 200)
        self.assertTrue(store.settings(self.root)['enabled'])

    def test_preferences_are_scoped_to_current_user_and_validate_headers(self):
        self.assertEqual(self.request('/api/notifications', dict(email='user@example.com', enabled=True))[0], 200)
        self.assertEqual(auth.notification_preferences(self.base, self.user)['email'], 'user@example.com')
        self.assertFalse(auth.notification_preferences(self.base, self.owner)['enabled'])
        for value in (dict(email='', enabled=True), dict(email='x@example.com\r\nBcc: other@example.com', enabled=True),
                      dict(email='a@example.com,b@example.com', enabled=True), dict(email='a@example.com', enabled=True, user_id=1)):
            self.assertEqual(self.request('/api/notifications', value)[0], 400)
        self.assertEqual(self.request('/api/notifications')[1]['email'], 'user@example.com')

    def test_qq_smtp_settings_private_and_secret_retained_only_for_same_sender(self):
        self.current[0] = self.owner
        payload = dict(enabled=True, sender='sender@qq.com', authorization_code='abcdefghijklmnop')
        status, data = self.request('/api/admin/mail', payload)
        self.assertEqual(status, 200)
        self.assertTrue(data['has_authorization_code'])
        self.assertNotIn('abcdefghijklmnop', json.dumps(data))
        self.assertEqual((self.base/notifications.MAIL_FILE).stat().st_mode & 0o777, 0o600)
        self.assertNotIn('authorization_code', self.request('/api/admin/mail')[1])
        self.assertEqual(self.request('/api/admin/mail', dict(payload, enabled=False, authorization_code=''))[0], 200)
        self.assertEqual(notifications.load_mail(self.base)['authorization_code'], 'abcdefghijklmnop')
        self.assertEqual(self.request('/api/admin/mail', dict(payload, sender='other@qq.com', authorization_code=''))[0], 400)
        self.assertEqual(self.request('/api/admin/mail', dict(payload, sender='user@example.com'))[0], 400)

    def test_error_email_deduplicated_no_raw_credentials_or_response(self):
        self.configure_mail()
        run_id = store.begin(self.root, None)
        report = dict(errors=[dict(kind='auth_invalid', summary='SECRET_RAW_TOKEN https://example.com/?access_token=secret')])
        callback = notifications.callback(self.base, self.user, self.root)
        automation.finish_run(self.root, run_id, report, 'error', callback)
        automation.finish_run(self.root, run_id, report, 'error', callback)
        smtp = self.smtp.return_value.__enter__.return_value
        smtp.send_message.assert_called_once()
        message = smtp.send_message.call_args.args[0]
        body = message.get_content()
        self.assertIn('更新 token', body)
        self.assertNotIn('SECRET_RAW_TOKEN', body)
        self.assertNotIn('access_token=', body)
        self.assertEqual(message['To'], 'recipient@example.com')
        self.assertEqual(self.smtp.call_args.args, ('smtp.qq.com', 465))
        self.assertEqual(self.smtp.call_args.kwargs['context'].verify_mode, 2)
        self.assertEqual(store.history(self.root)['items'][0]['email_status'], 'sent')

    def test_smtp_failure_does_not_change_game_failure_or_retry_submission(self):
        self.configure_mail()
        self.smtp.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'SECRET_SMTP_ERROR')
        run_id = store.begin(self.root, None)
        automation.finish_run(self.root, run_id, {}, 'error', notifications.callback(self.base, self.user, self.root))
        item = store.history(self.root)['items'][0]
        self.assertEqual(item['outcome'], 'error')
        self.assertEqual(item['email_status'], 'failed')
        self.assertNotIn('SECRET_SMTP_ERROR', json.dumps(item))
        self.smtp.return_value.__enter__.return_value.send_message.assert_not_called()

    def test_no_email_on_success_disabled_preference_or_check_only(self):
        self.configure_mail()
        for outcome, report in (('success', {}), ('skipped', {}), ('error', dict(check_only=True))):
            notifications.notify_error(self.base, self.user, self.root, 1, report, outcome)
        auth.notification_preferences(self.base, self.user, dict(email='', enabled=False))
        notifications.notify_error(self.base, self.user, self.root, 1, {}, 'error')
        self.smtp.assert_not_called()

    def test_partial_run_failure_sends_once_and_notification_error_is_independent(self):
        self.configure_mail()
        (self.root/'config-dashboard.json').write_text(json.dumps(app.importer.parse_link(sample_link())[0]))
        with patch.object(helper, 'sign_status', return_value=True), patch.object(helper, 'tasks',
                return_value=[dict(id=1, type=1, name='登录游戏', status=1)]), patch.object(helper, 'request',
                side_effect=helper.ApiError('模拟未确认', 'unconfirmed')) as submit:
            self.assertEqual(self.request('/api/run', {})[0], 409)
            submit.assert_called_once()
        self.smtp.return_value.__enter__.return_value.send_message.assert_called_once()
        run_id = store.begin(self.root, None)
        def broken(*args): raise OSError('must-not-propagate')
        automation.finish_run(self.root, run_id, {}, 'success', broken)
        self.assertEqual(store.history(self.root)['items'][0]['outcome'], 'success')

    def test_missing_global_mail_is_reported_without_changing_game_result(self):
        auth.notification_preferences(self.base, self.user, dict(email='recipient@example.com', enabled=True))
        run_id = store.begin(self.root, None)
        automation.finish_run(self.root, run_id, {}, 'error', notifications.callback(self.base, self.user, self.root))
        self.assertEqual(store.history(self.root)['items'][0]['email_status'], 'not_configured')
        self.smtp.assert_not_called()

    def test_manual_and_scheduled_errors_use_same_notification_hook(self):
        self.configure_mail()
        store.settings(self.root, dict(store.DEFAULT, enabled=True))
        (self.root/'config-dashboard.json').write_text(json.dumps(app.importer.parse_link(sample_link())[0]))
        with patch.object(helper, 'sign_status', side_effect=helper.ApiError('请更新 token', 'auth_invalid')):
            self.assertEqual(self.request('/api/run', {})[0], 500)
            self.assertEqual(automation.execute_user(self.root, helper, run_now=True,
                notify=notifications.callback(self.base, self.user, self.root)), 1)
        self.assertEqual(self.smtp.return_value.__enter__.return_value.send_message.call_count, 2)
        self.assertEqual([r['outcome'] for r in store.history(self.root)['items']], ['error', 'error'])

    def test_delete_requires_admin_and_exact_username_confirmation(self):
        payload = dict(id=self.user['id'], username=self.user['username'])
        for actor, expected in ((None, 401), (self.user, 403)):
            self.current[0] = actor
            self.assertEqual(self.request('/api/admin/users/delete', payload)[0], expected)
        self.current[0] = self.owner
        for bad in (dict(payload, username='other'), dict(payload, id=True), dict(payload, id='../1'),
                    dict(payload, id=-1), dict(payload, id=2**64), dict(payload, force=True), {}):
            self.assertEqual(self.request('/api/admin/users/delete', bad)[0], 400)
        self.assertTrue(auth.user_exists(self.base, self.user))
        self.assertTrue(self.root.exists())

    def test_delete_revokes_sessions_cleans_data_and_preserves_other_accounts(self):
        token = auth.new_session(self.base, self.user)
        self.configure_mail()
        store.settings(self.root, dict(store.DEFAULT, enabled=True))
        run_id = store.begin(self.root, None)
        store.update(self.root, run_id, dict(trigger='scheduled'), 'success')
        (self.root/'config-dashboard.json').write_text('private fake token')
        (self.root/'pending-operation.json').write_text('{}')
        owner_token = auth.new_session(self.base, self.owner)
        self.current[0] = self.owner
        result, data = self.request('/api/admin/users/delete', dict(id=self.user['id'], username='tester'))
        self.assertEqual(result, 200)
        self.assertFalse(data['cleanup_pending'])
        self.assertFalse(self.root.exists())
        self.assertFalse(auth.user_exists(self.base, self.user))
        self.assertIsNone(auth.session_user(self.base, token))
        self.assertIsNone(auth.authenticate(self.base, 'tester', 'PrivateUserPassword123'))
        with self.assertRaises(ValueError): auth.new_session(self.base, self.user)
        self.assertTrue(auth.session_user(self.base, owner_token))
        self.assertTrue((self.base/'panel.key').is_file())
        self.assertTrue(notifications.public_mail(self.base)['ready'])
        self.assertEqual(self.request('/api/admin/users')[1]['total'], 1)
        self.assertEqual(self.request('/api/admin/users/delete', dict(id=self.user['id'], username='tester'))[0], 404)

    def test_current_and_initial_accounts_are_protected(self):
        self.current[0] = self.owner
        rows = self.request('/api/admin/users')[1]['items']
        self.assertFalse(rows[0]['delete_allowed'])
        self.assertTrue(rows[1]['delete_allowed'])
        self.assertEqual(self.request('/api/admin/users/delete', dict(id=self.owner['id'], username='ark'))[0], 409)
        with closing(auth.connect(self.base)) as db, db:
            db.execute('UPDATE users SET is_admin=1 WHERE id=?', (self.user['id'],))
        self.current[0] = dict(self.user, is_admin=1)
        for target in (self.owner, self.user):
            self.assertEqual(self.request('/api/admin/users/delete', dict(id=target['id'], username=target['username']))[0], 409)

    def test_busy_account_is_not_deleted_until_operation_finishes(self):
        self.current[0] = self.owner
        lock = auth.operation_lock(self.base, self.user)
        self.assertTrue(lock.acquire())
        payload = dict(id=self.user['id'], username='tester')
        try:
            self.assertEqual(self.request('/api/admin/users/delete', payload)[0], 409)
            self.assertTrue(auth.user_exists(self.base, self.user))
            self.assertTrue(self.root.exists())
        finally: lock.release()
        self.assertEqual(self.request('/api/admin/users/delete', payload)[0], 200)

    def test_deleted_account_cannot_run_queued_work_recreate_storage_or_reuse_id(self):
        self.current[0] = self.owner
        self.assertEqual(self.request('/api/admin/users/delete', dict(id=self.user['id'], username='tester'))[0], 200)
        # 模拟删除前已经取到身份和目录的 HTTP 请求及调度列表快照。
        self.current[0] = self.user
        with patch.object(helper, 'sign_status') as game:
            for path, body in (('/api/status', None), ('/api/history', None), ('/api/schedule', {}),
                               ('/api/import', dict(url=sample_link())), ('/api/run', {})):
                self.assertEqual(self.request(path, body)[0], 401)
            self.assertEqual(automation.execute_user(self.root, helper, run_now=True,
                            account=(self.base, self.user)), 0)
            game.assert_not_called()
        self.assertFalse(self.root.exists())
        replacement = auth.create_user(self.base, 'tester', 'OtherPrivatePassword123')
        self.assertGreater(replacement['id'], self.user['id'])
        self.assertNotEqual(auth.user_root(self.base, replacement), self.root)

    def test_delete_disk_failure_still_revokes_identity_and_reports_cleanup_pending(self):
        self.current[0] = self.owner
        token = auth.new_session(self.base, self.user)
        with patch.object(admin.shutil, 'rmtree', side_effect=PermissionError('SECRET_OS_ERROR')):
            status, data = self.request('/api/admin/users/delete', dict(id=self.user['id'], username='tester'))
        self.assertEqual(status, 200)
        self.assertTrue(data['cleanup_pending'])
        self.assertNotIn('SECRET_OS_ERROR', json.dumps(data))
        self.assertIsNone(auth.session_user(self.base, token))
        self.assertFalse(auth.user_exists(self.base, self.user))

    def test_delete_preserves_id_watermark_when_upgrading_old_database(self):
        with closing(auth.connect(self.base)) as db, db:
            db.execute('DELETE FROM user_sequence')
        self.current[0] = self.owner
        self.assertEqual(self.request('/api/admin/users/delete', dict(id=self.user['id'], username='tester'))[0], 200)
        replacement = auth.create_user(self.base, 'another', 'OtherPrivatePassword123')
        self.assertGreater(replacement['id'], self.user['id'])

    def test_scheduler_checks_enabled_plan_inside_account_lock(self):
        (self.root/'config-dashboard.json').write_text('{}')
        with patch.object(helper, 'sign_status') as game:
            self.assertEqual(automation.execute_user(self.root, helper, run_now=True,
                            account=(self.base, self.user)), 0)
            game.assert_not_called()
        self.assertEqual(store.history(self.root)['items'], [])

    def test_admin_counts_only_scheduled_runs_and_does_not_create_account_storage(self):
        for trigger, outcome, check_only in (('scheduled','success',False),('scheduled','error',False),
                ('scheduled_now','interrupted',False),('manual','success',False),('scheduled','success',True),('scheduled','skipped',False)):
            run_id = store.begin(self.root, None)
            store.update(self.root, run_id, dict(trigger=trigger, check_only=check_only), outcome)
        (self.root/'config-dashboard.json').write_text('{"secret":"must-not-read"}')
        self.current[0] = self.owner
        status, data = self.request('/api/admin/users?limit=1&offset=1')
        self.assertEqual(status, 200)
        user = data['items'][0]
        self.assertTrue(user['configured'])
        self.assertEqual(user['execution']['scheduled_runs'], 3)
        self.assertEqual(user['execution']['successes'], 1)
        self.assertEqual(user['execution']['failures'], 1)
        self.assertEqual(user['execution']['interrupted'], 1)
        self.assertNotIn('must-not-read', json.dumps(data))
        missing = self.base/'never-created'
        self.assertEqual(admin.execution_summary(missing)['scheduled_runs'], 0)
        self.assertFalse(missing.exists())

    def test_migrates_legacy_schema_without_losing_sessions_or_passwords(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with sqlite3.connect(root/'users.sqlite3') as db:
                db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,password TEXT,created INTEGER,legacy INTEGER)')
                db.execute("INSERT INTO users VALUES (1,'ark','opaque-hash',123,1)")
                db.execute("INSERT INTO users VALUES (2,'other','other-hash',124,0)")
            with closing(auth.connect(root)) as db:
                rows = [dict(row) for row in db.execute('SELECT * FROM users ORDER BY id')]
            self.assertEqual(rows[0]['password'], 'opaque-hash')
            self.assertEqual(rows[0]['is_admin'], 1)
            self.assertEqual(rows[1]['is_admin'], 0)
            self.assertEqual(rows[1]['email'], '')


if __name__ == '__main__': unittest.main()

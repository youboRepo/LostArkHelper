"""管理员账号管理；不读取游戏 token，不访问腾讯接口。"""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import shutil

import auth
import store


def execution_summary(root):
    empty = dict(scheduled_runs=0, successes=0, failures=0, interrupted=0, running=0,
                 last_run=None, stats_available=True)
    file = Path(root) / 'automation.sqlite3'
    if not file.exists():
        return empty
    try:
        with closing(sqlite3.connect(file.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            # 统计真正的定时执行；部署只查询和锁冲突跳过不计入执行次数。
            where = "json_valid(details) AND json_extract(details,'$.trigger') IN ('scheduled','scheduled_now') AND coalesce(json_extract(details,'$.check_only'),0)=0 AND outcome!='skipped'"
            row = db.execute('SELECT COUNT(*) AS scheduled_runs,'
                             "coalesce(SUM(outcome='success'),0) AS successes,"
                             "coalesce(SUM(outcome IN ('error','blocked')),0) AS failures,"
                             "coalesce(SUM(outcome='interrupted'),0) AS interrupted,"
                             "coalesce(SUM(outcome='running'),0) AS running FROM runs WHERE " + where).fetchone()
            last = db.execute('SELECT id,started,finished,outcome FROM runs WHERE ' + where + ' ORDER BY id DESC LIMIT 1').fetchone()
        return dict(row, last_run=dict(last) if last else None, stats_available=True)
    except (sqlite3.Error, OSError):
        return dict(empty, stats_available=False)


def deletion_restriction(user, actor):
    if actor and user['id'] == actor['id']:
        return '当前登录账号不能删除。'
    if user['legacy']:
        return '系统初始账号不能删除。'
    return ''


def users(base, limit=30, offset=0, actor=None):
    with closing(auth.connect(base)) as db:
        total = db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        rows = [dict(row) for row in db.execute('SELECT id,username,created,legacy,is_admin FROM users ORDER BY id LIMIT ? OFFSET ?', (limit, offset))]
    items = []
    for user in rows:
        root = auth.user_root(base, user, create=False)
        schedule = dict(store.DEFAULT)
        file = root / 'automation.sqlite3'
        schedule_available = True
        if file.exists():
            try:
                with closing(sqlite3.connect(file.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
                    row = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
                if row:
                    schedule = store.validate(json.loads(row[0]))
            except (sqlite3.Error, OSError, ValueError, TypeError):
                schedule_available = False
        items.append(dict(id=user['id'], username=user['username'],
                          created=datetime.fromtimestamp(user['created'], store.TZ).isoformat(),
                          is_admin=bool(user['is_admin']), configured=(root/'config-dashboard.json').is_file(),
                          delete_allowed=not bool(deletion_restriction(user, actor)),
                          delete_reason=deletion_restriction(user, actor),
                          schedule=schedule if schedule_available else None,
                          execution=execution_summary(root)))
    return dict(items=items, total=total, offset=offset, limit=limit,
                next_offset=offset+limit if offset+limit < total else None)


class DeleteError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def delete_user(base, actor, value):
    """持账号锁撤销身份及会话，再清理私有数据；正在执行时不强制中断游戏请求。"""
    if (not isinstance(value, dict) or set(value) != {'id', 'username'} or
            type(value['id']) is not int or not 1 <= value['id'] <= 9223372036854775807 or
            not isinstance(value['username'], str)):
        raise DeleteError(400, '删除参数无效，请重新选择账号并输入用户名确认。')
    with closing(auth.connect(base)) as db:
        row = db.execute('SELECT id,username,legacy,is_admin FROM users WHERE id=?', (value['id'],)).fetchone()
    if not row:
        raise DeleteError(404, '账号已不存在，请刷新列表。')
    target = dict(row)
    reason = deletion_restriction(target, actor)
    if reason:
        raise DeleteError(409, reason)
    if value['username'] != target['username']:
        raise DeleteError(400, '确认用户名不匹配，未删除账号。')
    lock = auth.operation_lock(base, target)
    if not lock.acquire():
        raise DeleteError(409, '该账号正在执行或处理其他操作，请等待结束后再删除。')
    try:
        with closing(auth.connect(base)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            # 管理员本身也可能在另一个请求中被删除，必须在提交事务中重新验证。
            current = db.execute('SELECT is_admin FROM users WHERE id=?', (actor['id'],)).fetchone()
            if not current or not current['is_admin']:
                raise DeleteError(403, '管理员权限已失效，请重新登录。')
            row = db.execute('SELECT id,username,legacy FROM users WHERE id=?', (target['id'],)).fetchone()
            if not row:
                raise DeleteError(404, '账号已不存在，请刷新列表。')
            if deletion_restriction(row, actor) or row['username'] != value['username']:
                raise DeleteError(409, '账号状态已改变，请刷新列表。')
            # 兼容升级后尚未创建过新账号的旧库，删除最高 ID 前保存水位。
            highest = db.execute('SELECT MAX(id) FROM users').fetchone()[0]
            db.execute('INSERT OR IGNORE INTO user_sequence VALUES (1,?)', (highest,))
            db.execute('UPDATE user_sequence SET last_id=MAX(last_id,?) WHERE id=1', (highest,))
            db.execute('DELETE FROM sessions WHERE user_id=?', (target['id'],))
            db.execute('DELETE FROM attempts WHERE key=?', ('name:' + target['username'],))
            db.execute('DELETE FROM users WHERE id=?', (target['id'],))
        # 永久撤销身份后再清理，磁盘清理失败也不能让账号继续登录或定时执行。
        root = auth.user_root(base, target, create=False)
        cleanup_pending = False
        try:
            if root.is_symlink():
                root.unlink()
            elif root.exists():
                shutil.rmtree(root)
        except OSError:
            cleanup_pending = True
        message = '账号 {} 已删除，登录会话、定时任务和私有数据已清理。'.format(target['username'])
        if cleanup_pending:
            message = '账号 {} 已删除，登录及定时任务已停用；部分私有文件清理失败，请管理员检查私有目录 users/{}。'.format(target['username'], target['id'])
        return dict(message=message, cleanup_pending=cleanup_pending)
    finally:
        lock.release()

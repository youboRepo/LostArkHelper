"""Private SQLite scheduling settings and append-only execution history."""
from contextlib import contextmanager
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
DEFAULT = {'enabled': False, 'mode': 'hourly', 'minute': 0, 'times': ['09:00']}

def now():
    return datetime.now(TZ)

# 每个账号目录拥有独立 SQLite。settings 保存计划，runs 保存运行记录，slot 唯一约束避免同一计划重复执行。
@contextmanager
def connect(root):
    db = sqlite3.connect(str(Path(root) / 'automation.sqlite3'), timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, slot TEXT UNIQUE, started TEXT NOT NULL,
          finished TEXT, outcome TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mail_deliveries (
          run_id INTEGER PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '');
    ''')
    # sqlite3.Connection 的 with 只提交/回滚，不会 close。
    # 显式关闭连接，释放文件句柄，不能依赖垃圾回收。
    try:
        with db:
            yield db
    finally:
        db.close()

# 严格校验前端提交的计划字段、分钟及 HH:MM 格式，不能直接信任网页输入。
def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValueError('invalid settings')
    if type(value['enabled']) is not bool or value['mode'] not in ('hourly', 'daily'):
        raise ValueError('invalid mode')
    if type(value['minute']) is not int or not 0 <= value['minute'] <= 59:
        raise ValueError('invalid minute')
    times = value['times']
    if not isinstance(times, list) or not 1 <= len(times) <= 24:
        raise ValueError('invalid times')
    import re
    if any(not isinstance(t, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', t) for t in times):
        raise ValueError('invalid time')
    return dict(value, times=sorted(set(times)))

def settings(root, value=None):
    with connect(root) as db:
        if value is not None:
            value = validate(value)
            db.execute('INSERT OR REPLACE INTO settings VALUES (1, ?)', (json.dumps(value),))
        row = db.execute('SELECT value FROM settings WHERE id=1').fetchone()
    return json.loads(row['value']) if row else dict(DEFAULT)


def token_imported(root, first_import):
    """首次成功导入选择一次随机分钟；更新旧凭据不覆盖已有计划。调用方持账号锁。"""
    with connect(root) as db:
        db.execute('BEGIN IMMEDIATE')
        initialized = db.execute("SELECT 1 FROM account_state WHERE key='token_imported'").fetchone()
        changed = first_import and not initialized
        if changed:
            value = dict(DEFAULT, enabled=True, minute=secrets.randbelow(60))
            db.execute('INSERT OR REPLACE INTO settings VALUES (1,?)', (json.dumps(value),))
        db.execute("INSERT OR IGNORE INTO account_state VALUES ('token_imported','1')")
    return changed

# 用于页面显示下一次计划时间，不负责实际启动任务。
def next_run(value, at=None):
    if not value['enabled']:
        return None
    at = at or now()
    for offset in range(1441):
        candidate = (at + timedelta(minutes=offset)).replace(second=0, microsecond=0)
        if candidate <= at:
            continue
        if due(value, candidate):
            return candidate.isoformat()
    return None

# 判断某一分钟是否匹配计划；时间按项目固定的北京时间 UTC+8。
def due(value, at):
    return value['enabled'] and (at.minute == value['minute'] if value['mode'] == 'hourly'
                                 else at.strftime('%H:%M') in value['times'])

# 按记录 ID 倒序分页，每页最多 30 条，不返回游戏凭据。
def history(root, before=None):
    with connect(root) as db:
        rows = db.execute('SELECT runs.*,mail_deliveries.state AS email_status,mail_deliveries.reason AS email_reason '
                          'FROM runs LEFT JOIN mail_deliveries ON runs.id=mail_deliveries.run_id '
                          'WHERE runs.id < ? ORDER BY runs.id DESC LIMIT 31',
                          (before or 9223372036854775807,)).fetchall()
    items = [dict(row, details=json.loads(row['details'])) for row in rows[:30]]
    return {'items': items, 'next': items[-1]['id'] if len(rows) > 30 else None}

# 更新执行详情；传入最终 outcome 时，同时记录结束时间。
def update(root, run_id, details, outcome=None):
    with connect(root) as db:
        if outcome:
            db.execute('UPDATE runs SET details=?, outcome=?, finished=? WHERE id=?',
                       (json.dumps(details, ensure_ascii=False), outcome, now().isoformat(), run_id))
        else:
            db.execute('UPDATE runs SET details=? WHERE id=?', (json.dumps(details, ensure_ascii=False), run_id))

# 调用方必须先持有操作锁；标记此前未结束的运行，再尝试写入唯一时间槽。
def begin(root, slot):
    with connect(root) as db:
        # Caller owns the process lock: previous running rows were interrupted.
        db.execute("UPDATE runs SET outcome='interrupted', finished=? WHERE outcome='running'", (now().isoformat(),))
        cursor = db.execute("INSERT OR IGNORE INTO runs(slot,started,outcome,details) VALUES (?,?,'running','{}')",
                            (slot, now().isoformat()))
        return cursor.lastrowid if cursor.rowcount else None

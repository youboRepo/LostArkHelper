"""Local users and expiring, revocable sessions; no plaintext passwords."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SESSION_SECONDS = 7 * 24 * 3600

def connect(root):
    db = sqlite3.connect(str(Path(root) / 'users.sqlite3'), timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        created INTEGER NOT NULL, legacy INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS sessions (
        digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS attempts (
        key TEXT NOT NULL, at INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS attempts_key_at ON attempts(key,at);
      CREATE TABLE IF NOT EXISTS user_sequence (
        id INTEGER PRIMARY KEY CHECK(id=1), last_id INTEGER NOT NULL);
    ''')
    # 原有数据库原地增加权限和通知偏好；注册入口不能指定管理员身份。
    additions = {'is_admin': 'INTEGER NOT NULL DEFAULT 0',
                 'email': "TEXT NOT NULL DEFAULT ''", 'notify_errors': 'INTEGER NOT NULL DEFAULT 0'}
    columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
    if not additions.keys() <= columns:
        with db:
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
            for name, definition in additions.items():
                if name not in columns:
                    db.execute('ALTER TABLE users ADD COLUMN ' + name + ' ' + definition)
            db.execute("UPDATE users SET is_admin=1 WHERE username='ark' AND legacy=1")
    return db

# 使用独立随机盐与 600000 轮 PBKDF2；数据库保存哈希，不能保存用户明文密码。
def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return salt + ':' + value

def verify(password, encoded):
    return hmac.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)

def create_user(root, username, password, legacy=False):
    if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z0-9_]{3,32}', username):
        raise ValueError('用户名需为 3–32 位字母、数字或下划线。')
    username = username.lower()
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError('密码需为 12–128 个字符。')
    encoded = password_hash(password)
    try:
        with closing(connect(root)) as db, db:
            # 删除最高 ID 后也不能复用它，否则排队的旧请求可能指向新账号。
            db.execute('BEGIN IMMEDIATE')
            last = db.execute('SELECT last_id FROM user_sequence WHERE id=1').fetchone()
            uid = max(last[0] if last else 0, db.execute('SELECT coalesce(MAX(id),0) FROM users').fetchone()[0]) + 1
            db.execute('INSERT OR REPLACE INTO user_sequence VALUES (1,?)', (uid,))
            db.execute('INSERT INTO users(id,username,password,created,legacy,is_admin) VALUES (?,?,?,?,?,?)',
                       (uid, username, encoded, int(time.time()), int(legacy), int(legacy)))
        return {'id': uid, 'username': username, 'legacy': int(legacy), 'is_admin': int(legacy)}
    except sqlite3.IntegrityError:
        raise ValueError('该用户名不可用，请换一个。') from None

# 兼容原始 ark 账号的初始化。新部署必须预先准备私有 panel.key 文件；不要随源码分发。
def initialize(root):
    Path(root).mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(connect(root)) as db:
        exists = db.execute("SELECT 1 FROM users WHERE username='ark'").fetchone()
    if not exists:
        # Reserve the original account before accepting public registration.
        password = (Path(root) / 'panel.key').read_text().strip()
        create_user(root, 'ark', password, legacy=True)

# 用服务端生成的用户数字 ID 隔离目录，不用用户输入的名字拼文件路径。
def user_root(root, user, create=True):
    if user['legacy']:
        return Path(root)
    # Only server-generated integer IDs determine private paths.
    path = Path(root) / 'users' / str(int(user['id']))
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path

def all_users(root):
    with closing(connect(root)) as db:
        return [dict(r) for r in db.execute('SELECT id,username,legacy,created,is_admin FROM users ORDER BY id')]


def user_exists(root, user):
    with closing(connect(root)) as db:
        return db.execute('SELECT 1 FROM users WHERE id=?', (user['id'],)).fetchone() is not None


def operation_lock(root, user):
    # 锁在账号目录之外。删除账号数据后旧请求仍争用同一把锁，再检查账号是否存在。
    from automation import OperationLock
    folder = Path(root) / 'account-locks'
    folder.mkdir(mode=0o700, exist_ok=True)
    return OperationLock(folder, filename='{}.lock'.format(int(user['id'])))

def authenticate(root, username, password):
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
        return None
    with closing(connect(root)) as db:
        row = db.execute('SELECT * FROM users WHERE username=?', (username.lower(),)).fetchone()
    # Perform the same expensive hash for missing users.
    encoded = row['password'] if row else '00'*16 + ':' + '00'*64
    if not verify(password, encoded):
        return None
    return {key: row[key] for key in ('id', 'username', 'legacy', 'is_admin')} if row else None

# 给浏览器随机会话令牌；数据库只存其 SHA-256 摘要和过期时间。
def new_session(root, user):
    token = secrets.token_urlsafe(32)
    at = int(time.time())
    with closing(connect(root)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM users WHERE id=?', (user['id'],)).fetchone():
            raise ValueError('账号已删除。')
        db.execute('DELETE FROM sessions WHERE expires<=?', (at,))
        db.execute('INSERT INTO sessions VALUES (?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(), user['id'], at + SESSION_SECONDS))
    return token

def session_user(root, token):
    if not token or len(token) > 128:
        return None
    with closing(connect(root)) as db:
        row = db.execute('SELECT u.id,u.username,u.legacy,u.is_admin FROM sessions s JOIN users u ON u.id=s.user_id WHERE digest=? AND expires>?',
                         (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
    return dict(row) if row else None


def public_user(user):
    return {'username': user['username'], 'is_admin': bool(user.get('is_admin', False))} if user else None


def notification_preferences(root, user, value=None):
    """仅操作当前登录用户的收件邮箱，不接受请求体中的用户 ID。"""
    with closing(connect(root)) as db, db:
        if value is not None:
            db.execute('UPDATE users SET email=?,notify_errors=? WHERE id=?',
                       (value['email'], int(value['enabled']), user['id']))
        row = db.execute('SELECT email,notify_errors FROM users WHERE id=?', (user['id'],)).fetchone()
    if row is None:
        raise ValueError('账号不存在。')
    return {'email': row['email'], 'enabled': bool(row['notify_errors'])}

def logout(root, token):
    with closing(connect(root)) as db, db:
        db.execute('DELETE FROM sessions WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),))

# 记录认证尝试并限流，限制按 IP 和用户名维度的频繁登录/注册。
def rate_limit(root, identity, username, registering=False):
    at = int(time.time())
    keys = [('ip:' + identity, 15 if not registering else 5),
            ('name:' + str(username).lower()[:32], 15)]
    with closing(connect(root)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM attempts WHERE at < ?', (at - 900,))
        for key, limit in keys:
            count = db.execute('SELECT COUNT(*) FROM attempts WHERE key=?', (key,)).fetchone()[0]
            if count >= limit:
                return False
        for key, _ in keys:
            db.execute('INSERT INTO attempts VALUES (?,?)', (key, at))
    return True

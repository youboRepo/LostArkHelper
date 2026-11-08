"""One website user may bind several game accounts; each has private credentials and pending-operation guard.

Layout inside a user's private root:
  <root>/config-dashboard.json            account "1" (legacy single-account location, unchanged)
  <root>/accounts/<id>/config-dashboard.json   accounts "2".."N"
Each account directory also owns meta.json (display label), pending-operation.json and operations.lock,
so one account's unconfirmed operation or running job never blocks the others.
Schedule and run history stay per website user (store.py); runs carry an account id.
"""
import json
import os
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

MAX_ACCOUNTS = 10
CONFIG = 'config-dashboard.json'
FIRST = '1'
_ID = re.compile(r'[1-9][0-9]{0,2}')

def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None

# 账号 ID 只允许服务端生成的数字，绝不用用户输入拼路径。
def account_root(user_root, aid):
    if not valid_id(aid):
        raise ValueError('账号编号无效')
    return Path(user_root) if aid == FIRST else Path(user_root) / 'accounts' / aid

def read_config(root):
    obj = json.loads((Path(root) / CONFIG).read_text(encoding='utf-8'))
    if not isinstance(obj, dict) or not isinstance(obj.get('form'), dict) or not isinstance(obj.get('cookie'), str):
        raise ValueError('invalid')
    return obj

def read_meta(root):
    try:
        meta = json.loads((Path(root) / 'meta.json').read_text(encoding='utf-8'))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}

def clean_label(value, fallback):
    if not isinstance(value, str):
        return fallback
    text = ''.join(c for c in value if c.isprintable()).strip()[:30]
    return text or fallback

def write_meta(root, label, info=None):
    from automation import save_state  # local import: automation imports this module
    old = read_meta(root)
    meta = {'label': label, 'role': old.get('role', ''), 'server': old.get('server', ''), 'area': old.get('area', '')}
    if info:
        meta.update({k: str(info.get(k) or '')[:100] for k in ('role', 'server', 'area')})
    save_state(Path(root) / 'meta.json', meta)

def _entry(user_root, aid):
    root = account_root(user_root, aid)
    meta = read_meta(root)
    label = clean_label(meta.get('label'), '账号 ' + aid)
    return {'id': aid, 'root': root, 'label': label, 'role': meta.get('role', ''),
            'server': meta.get('server', ''), 'area': meta.get('area', '')}

# 只列出已导入凭据的账号，按编号排序。
def list_accounts(user_root):
    ids = []
    if (Path(user_root) / CONFIG).is_file():
        ids.append(FIRST)
    base = Path(user_root) / 'accounts'
    if base.is_dir():
        ids += [p.name for p in base.iterdir() if valid_id(p.name) and p.name != FIRST and (p / CONFIG).is_file()]
    return [_entry(user_root, aid) for aid in sorted(ids, key=int)]

def get(user_root, aid):
    if not valid_id(aid) or not (account_root(user_root, aid) / CONFIG).is_file():
        return None
    return _entry(user_root, aid)

# 同一游戏角色重复绑定会导致重复提交，创建前按 user_id + 角色 ID 去重。
def identity(config):
    form = config.get('form', {})
    return str(form.get('user_id')), str(form.get('sRoleld'))

def find_duplicate(user_root, config, ignore=None):
    for acct in list_accounts(user_root):
        if acct['id'] == ignore:
            continue
        try:
            if identity(read_config(acct['root'])) == identity(config):
                return acct
        except (OSError, ValueError):
            continue
    # Pending retirements remain private and block rebinding the same game character.
    base = Path(user_root) / 'quarantine'
    if base.is_dir():
        for root in base.iterdir():
            if not root.is_dir() or not valid_id(root.name) or not (root / 'pending-operation.json').is_file():
                continue
            try:
                if identity(read_config(root)) == identity(config):
                    return {'id': root.name, 'root': root, 'label': clean_label(read_meta(root).get('label'), '账号 ' + root.name), 'quarantined': True}
            except (OSError, ValueError):
                # Fail closed: a damaged quarantine entry must not silently permit rebinding.
                raise ValueError('隔离账号记录异常，请联系管理员。')
    return None

def issued_floor(user_root):
    """Highest known issued account number, including older history and orphan directories."""
    root = Path(user_root)
    base = root / 'accounts'
    counter = base / '.next'
    floor = 0
    if counter.exists():
        floor = int(counter.read_text().strip())
        if not 0 <= floor <= 999:
            raise ValueError('账号编号计数器无效')
    if (root / CONFIG).is_file():
        floor = max(floor, 1)
    if base.is_dir():
        floor = max([floor] + [int(p.name) for p in base.iterdir() if p.is_dir() and valid_id(p.name)])
    archived = root / 'quarantine'
    if archived.is_dir():
        floor = max([floor] + [int(p.name) for p in archived.iterdir() if p.is_dir() and valid_id(p.name)])
    db_path = root / 'automation.sqlite3'
    if db_path.is_file():
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            columns = {r[1] for r in db.execute('PRAGMA table_info(runs)')}
            if columns and 'account' in columns:
                for (aid,) in db.execute('SELECT DISTINCT account FROM runs'):
                    if not valid_id(aid):
                        raise ValueError('历史记录中的账号编号无效')
                    floor = max(floor, int(aid))
            elif columns and db.execute('SELECT 1 FROM runs LIMIT 1').fetchone():
                floor = max(floor, 1)
    return floor


def next_id(user_root):
    if len(list_accounts(user_root)) >= MAX_ACCOUNTS:
        raise ValueError('最多绑定 {} 个游戏账号，请先移除不用的账号。'.format(MAX_ACCOUNTS))
    nxt = issued_floor(user_root) + 1
    if nxt > 999:
        raise ValueError('账号编号已用尽，请联系管理员。')
    counter = Path(user_root) / 'accounts' / '.next'
    counter.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    from automation import save_state
    save_state(counter, nxt)
    return str(nxt)

# 调用方必须已持有该账号的锁（新建账号时目录尚不存在，由 app 持用户级创建锁）。
def save(user_root, aid, config, label, info=None):
    from automation import save_state
    root = account_root(user_root, aid)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    save_state(root / CONFIG, config)
    write_meta(root, label, info)

class PendingOperation(ValueError):
    pass

# 调用方必须已持有该账号的锁。存在待确认操作时拒绝移除：该记录阻止盲目重试，不能随账号一起丢失。
def remove(user_root, aid):
    root = account_root(user_root, aid)
    if (root / 'pending-operation.json').exists():
        raise PendingOperation('该账号有未确认的提交，请先在官方 App 核实并由管理员处理待确认记录后再移除。')
    # 删除前持久记录最高编号，兼容旧账号 1 尚无计数器的情况。
    counter = Path(user_root) / 'accounts' / '.next'
    counter.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    from automation import save_state
    save_state(counter, max(issued_floor(user_root), int(aid)))
    # 先删除凭据与状态文件（调用方正持有该目录的锁文件，在 Windows 上无法删除）。
    for name in (CONFIG, 'meta.json'):
        try:
            (root / name).unlink()
        except FileNotFoundError:
            pass
    if aid != FIRST:
        base = (Path(user_root) / 'accounts').resolve()
        if root.resolve().parent != base:
            raise ValueError('账号编号无效')
        # 只剩锁文件的空目录；没有 config 的目录不会被 list_accounts 识别为账号。
        shutil.rmtree(str(root), ignore_errors=True)


def quarantine_pending(user_root, aid):
    """Atomically retire a nonlegacy account while retaining its unresolved operation.

    Caller must hold both the user's accounts.lock and this account's operations.lock.
    This administrative helper never queries or submits to the game service.
    """
    if aid == FIRST:
        raise ValueError('旧版账号位置不能直接隔离')
    acct = get(user_root, aid)
    if not acct or not (acct['root'] / 'pending-operation.json').is_file():
        raise ValueError('账号不存在或没有待确认记录')
    base = Path(user_root) / 'quarantine'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = base / aid
    if dest.exists():
        raise ValueError('隔离位置已存在')
    counter = Path(user_root) / 'accounts' / '.next'
    counter.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    from automation import save_state
    save_state(counter, max(issued_floor(user_root), int(aid)))
    os.rename(str(acct['root']), str(dest))
    for directory in (counter.parent, base):
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return dest

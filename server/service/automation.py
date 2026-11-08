"""Hourly checks with a process-shared lock and durable mutation guard."""
import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
import store
import accounts
from datetime import datetime, timezone
from pathlib import Path


class OperationLock:
    def __init__(self, root, name='operations.lock'):
        self.path = Path(root) / name
        self.thread_lock = threading.Lock()
        self.fd = None

    # 同时取得线程锁和操作系统文件锁；文件存在不等于锁被占用，必须看锁操作是否成功。
    def acquire(self, blocking=False):
        if not self.thread_lock.acquire(blocking=blocking):
            return False
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            return True
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            self.thread_lock.release()
            return False
        except Exception:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self.thread_lock.release()
            raise

    # 关闭文件描述符即释放系统锁，然后允许同进程中的下一次操作。
    def release(self):
        os.close(self.fd)
        self.fd = None
        self.thread_lock.release()


@contextmanager
def execution_capacity(root):
    """Four shared execution slots for web and scheduler processes using one state root.

    Slot files are stable and must never be unlinked while services are running.
    Closing descriptors (including process exit) releases the OS locks.
    """
    while True:
        for number in range(4):
            lock = OperationLock(root, 'execution-{}.lock'.format(number))
            if lock.acquire():
                try:
                    yield
                finally:
                    lock.release()
                return
        time.sleep(0.05)


# 先写临时文件并 fsync，再原子替换目标文件，减少中断留下半份 JSON 的风险。
def save_state(path, state):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temp), str(path))
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# 一次完整执行：先查询，筛选可领取白名单任务，每次提交前后重新核对。结果未知立即停止，并用待确认记录阻止下次盲目重试。
def run(helper, config, root, check_only=False, report=None, allow_sign=True, allow_claim=True, db_root=None):
    # root 是该游戏账号的私有目录（待确认记录）；db_root 是所属网站用户目录（运行记录库），默认相同。
    db_root = db_root if db_root is not None else root
    report = report if report is not None else {}
    report.update(sign="not_checked", claimed=[], skipped=[], message="")
    report['errors'] = []
    report['stage'] = '查询签到状态'
    signed = helper.sign_status(config)
    report['sign'] = 'already_signed' if signed else 'unsigned'
    report['stage'] = '查询任务列表'
    items = helper.tasks(config)
    candidates = sorted(set(t['id'] for t in items
                            if t['status'] == 1 and t['id'] in helper.VERIFIED))
    unknown = sum(t['status'] == 1 and t['id'] not in helper.VERIFIED for t in items)
    print('CHECK signed={} claimable={} skipped_unsupported={}'.format(signed, candidates, unknown), flush=True)
    report['skipped'] = [{'id': t['id'], 'name': str(t.get('name', '未知任务'))[:100]} for t in items if t['status'] == 1 and t['id'] not in helper.VERIFIED]
    guard = Path(root) / 'pending-operation.json'
    if guard.exists():
        pending = json.loads(guard.read_text(encoding='utf-8'))
        action = pending.get('action')
        matched = [t for t in items if t['id'] == action]
        confirmed = signed if action == 'sign' else len(matched) == 1 and matched[0]['status'] == 2
        if confirmed:
            guard.unlink()
        else:
            raise helper.ApiError('上次提交的结果仍未确认，已禁止继续提交。请在官方 App 核实，并在暂停该账号计划并联系管理员核实待确认记录。', 'unconfirmed')
    if check_only:
        return 0

    # 提交一笔签到或奖励操作，并确认实际状态；结果未知时不能等同于失败后安全重试。
    def mutate(action, send, confirm):
        label = '签到' if action == 'sign' else '领取任务 #{}'.format(action)
        report['stage'] = label + '提交'
        if report.get('run_id'):
            store.update(db_root, report['run_id'], report)
        save_state(guard, {'action': action, 'started': store.now().isoformat()})
        try:
            send()
            report['stage'] = label + '结果确认'
            if not confirm():
                raise helper.ApiError('接口返回成功，但刷新后的状态未确认成功', 'unconfirmed')
        except Exception as exc:
            report['errors'].append(error_info(exc, report['stage']))
            if action == 'sign':
                report['sign'] = 'failed'
            return False
        guard.unlink()
        if action == 'sign':
            report['sign'] = 'signed_now'
        else:
            task = next(t for t in items if t['id'] == action)
            report['claimed'].append({'id': action, 'name': str(task.get('name', '未知任务'))[:100]})
        if report.get('run_id'):
            store.update(db_root, report['run_id'], report)
        print('CONFIRMED {}'.format(action), flush=True)
        return True

    if not signed and allow_sign:
        if not mutate('sign', lambda: helper.request(config, helper.SIGN_IN),
                      lambda: helper.sign_status(config)):
            report['message'] = '；'.join(e['summary'] for e in report['errors'])
            return 1
        # 本次签到可能刚好解锁累计签到奖励，刷新后再筛选候选。
        items = helper.tasks(config)
        candidates = sorted(set(t['id'] for t in items
                                if t['status'] == 1 and t['id'] in helper.VERIFIED))
    for tid in candidates if allow_claim else []:
        report['stage'] = '领取任务 #{} 前查询'.format(tid)
        current = [t for t in helper.tasks(config) if t['id'] == tid]
        if len(current) != 1 or current[0]['status'] != 1:
            continue
        def confirmed():
            after = [t for t in helper.tasks(config) if t['id'] == tid]
            return len(after) == 1 and after[0]['status'] == 2
        if not mutate(tid, lambda: helper.request(config, helper.CLAIM,
               {'task_id': str(tid), 'type': str(helper.VERIFIED[tid])}), confirmed):
            break
    report['stage'] = '执行完成'
    report['message'] = '；'.join(e['summary'] for e in report['errors'])
    return 1 if report['errors'] else 0


# 仅对经过 helper 脱敏边界的 ApiError 输出详情，未知异常只记录类型。
def error_info(exc, stage):
    # Only ApiError messages have passed the helper's redaction boundary.
    from helper import ApiError
    known = isinstance(exc, ApiError)
    kind = getattr(exc, 'kind', 'internal') if known else 'internal'
    message = str(exc) if known else '内部处理异常（{}）'.format(type(exc).__name__)
    code = getattr(exc, 'code', None) if known else None
    endpoint = getattr(exc, 'endpoint', None) if known else None
    summary = '{}：{}{}'.format(stage, message, ' [code={}]'.format(code) if code is not None else '')
    return dict(stage=stage, kind=kind, code=code, endpoint=endpoint, summary=summary)


# 手动操作入口（网页调用）：持该账号的锁、记录运行并返回 (HTTP 状态码, 提示文字)。
def manual_run(user_root, acct, helper, allow_sign=True, allow_claim=True, capacity_root=None):
    try:
        with execution_capacity(capacity_root if capacity_root is not None else user_root):
            return _manual_run(user_root, acct, helper, allow_sign, allow_claim)
    except Exception as exc:
        return 500, error_info(exc, '账号执行')['summary']


def _manual_run(user_root, acct, helper, allow_sign, allow_claim):
    lock = OperationLock(acct['root'])
    report = {'trigger': 'manual', 'account': {'id': acct['id'], 'label': acct['label']}}
    run_id = None
    acquired = False
    try:
        try:
            acquired = lock.acquire()
        except FileNotFoundError:
            return 409, '账号「{}」已移除，未执行。'.format(acct['label'])
        if not acquired:
            return 409, '账号「{}」已有操作正在进行，请稍后再试。'.format(acct['label'])
        if not accounts.get(user_root, acct['id']):
            return 409, '账号「{}」已移除，未执行。'.format(acct['label'])
        run_id = store.begin(user_root, None, acct['id'])
        report['run_id'] = run_id
        code = run(helper, accounts.read_config(acct['root']), acct['root'], report=report,
                   allow_sign=allow_sign, allow_claim=allow_claim, db_root=user_root)
        store.update(user_root, run_id, report, 'success' if code == 0 else 'error')
        if code != 0:
            return 409, report.get('message') or '执行失败，请查看运行记录。'
        return 200, '执行成功：' + ('本次签到成功' if report.get('sign') == 'signed_now' else '今日已签到，跳过签到') +             '；领取奖励 {} 项。'.format(len(report.get('claimed', [])))
    except Exception as exc:
        report.setdefault('errors', []).append(error_info(exc, report.get('stage', '手动操作')))
        report['message'] = '；'.join(e['summary'] for e in report['errors'])
        if run_id:
            store.update(user_root, run_id, report, 'error')
        return 500, report['message']
    finally:
        if acquired:
            lock.release()


# 账号级调度入口：持该游戏账号的锁、检查到期、按时间槽去重、读取配置，最后持久化脱敏运行结果。
# root 是网站用户目录（计划与运行记录）；acct 缺省时为账号 1（旧版单账号布局）。
def execute_user(root, helper, check_only=False, run_now=False, acct=None, due_at=None, capacity_root=None):
    # 绑定入队时刻；等待线程/全局额度不能把原本已到期的任务变成未到期。
    due_at = due_at if due_at is not None else store.now()
    try:
        with execution_capacity(capacity_root if capacity_root is not None else root):
            return _execute_user(root, helper, check_only, run_now, acct, due_at)
    except Exception:
        print('ERROR: account execution failed; other accounts remain independent. No immediate retry.', flush=True)
        return 1


def _execute_user(root, helper, check_only, run_now, acct, due_at):
    acct = acct or accounts.get(root, accounts.FIRST) or         {'id': accounts.FIRST, 'root': Path(root), 'label': '账号 1'}
    lock = OperationLock(acct['root'])
    run_id = None
    acquired = False
    report = {'check_only': check_only, 'trigger': 'scheduled_now' if run_now else 'scheduled',
              'account': {'id': acct['id'], 'label': acct['label']}}
    try:
        try:
            acquired = lock.acquire()
        except FileNotFoundError:
            print('SKIP: account {} was removed before execution.'.format(acct['id']), flush=True)
            return 0
        if not acquired:
            if not check_only and not run_now and store.due(store.settings(root), due_at):
                slot = due_at.strftime('%Y-%m-%dT%H:%M') + ('' if acct['id'] == '1' else '#' + acct['id'])
                at = store.now()
                with store.connect(root) as db:
                    db.execute("INSERT OR IGNORE INTO runs(slot,started,finished,outcome,details,account) VALUES (?,?,?,'skipped',?,?)",
                               (slot, at.isoformat(), at.isoformat(), json.dumps({'message':'其他操作正在执行，本次已跳过，避免重复提交。','account':{'id':acct['id'],'label':acct['label']}}, ensure_ascii=False), acct['id']))
            print('SKIP: another operation is running.', flush=True)
            return 0
        schedule = store.settings(root)
        if not check_only and not run_now and not store.due(schedule, due_at):
            return 0
        slot = due_at.strftime('%Y-%m-%dT%H:%M') if not check_only and not run_now else None
        run_id = store.begin(root, slot, acct['id'])
        if run_id is None:
            return 0
        report['run_id'] = run_id
        config_path = acct['root'] / 'config-dashboard.json'
        if not config_path.exists():
            report['message'] = '未导入面板账号，已跳过。'
            store.update(root, run_id, report, 'skipped')
            return 0
        config = json.loads(config_path.read_text(encoding='utf-8'))
        code = run(helper, config, acct['root'], check_only, report, db_root=root)
        store.update(root, run_id, report, 'success' if code == 0 else 'error')
        return code
    except Exception as exc:
        error = error_info(exc, report.get('stage', '读取账号配置'))
        report.setdefault('errors', []).append(error)
        report['message'] = '；'.join(e['summary'] for e in report['errors'])
        if run_id:
            store.update(root, run_id, report, 'error')
        print('ERROR: query/write failed; see private execution history. No immediate retry.', flush=True)
        return 1
    finally:
        if acquired:
            lock.release()


def main():
    import argparse
    import helper
    import auth
    from concurrent.futures import ThreadPoolExecutor
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--run-now', action='store_true', help='Run enabled schedules once now')
    args = parser.parse_args()
    root = Path(os.environ.get('ARK_HELPER_DIR', '/var/lib/ark-dashboard'))
    os.umask(0o077)
    auth.initialize(root)
    at = store.now()
    # 每个 (网站用户, 游戏账号) 是独立任务：各有自己的锁、待确认记录和运行记录，可并发。
    # 未到期/未启用的用户不会查询游戏接口；并发数固定上限，避免对官方接口突发大量请求。
    jobs = []
    for user in auth.all_users(root):
        user_root = auth.user_root(root, user)
        schedule = store.settings(user_root)
        if store.due(schedule, at) or (args.run_now and schedule['enabled']) or args.check_only:
            jobs += [(user_root, acct) for acct in accounts.list_accounts(user_root)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda job: execute_user(job[0], helper, args.check_only, args.run_now,
                                                       job[1], due_at=at, capacity_root=root), jobs))
    return 1 if any(code for code in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())

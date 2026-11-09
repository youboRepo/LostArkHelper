"""Hourly checks with a process-shared lock and durable mutation guard."""
import fcntl
import json
import os
import threading
import store
from datetime import datetime, timezone
from pathlib import Path


class OperationLock:
    def __init__(self, root, filename='operations.lock'):
        self.path = Path(root) / filename
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
def run(helper, config, root, check_only=False, report=None, allow_sign=True, allow_claim=True):
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
            raise helper.ApiError('上次提交的结果仍未确认。请前往账号管理更新 token；成功保存后将解除待确认限制，再执行时会重新查询任务状态。', 'unconfirmed')
    if check_only:
        return 0

    # 提交一笔签到或奖励操作，并确认实际状态；结果未知时不能等同于失败后安全重试。
    def mutate(action, send, confirm):
        label = '签到' if action == 'sign' else '领取任务 #{}'.format(action)
        report['stage'] = label + '提交'
        if report.get('run_id'):
            store.update(root, report['run_id'], report)
        save_state(guard, {'action': action, 'started': store.now().isoformat()})
        try:
            send()
            report['stage'] = label + '结果确认'
            if not confirm():
                raise helper.ApiError('接口返回成功，但刷新后的状态未确认成功', 'unconfirmed')
        except Exception as exc:
            info = error_info(exc, report['stage'])
            info['summary'] += '。请前往账号管理更新 token 后重试。'
            report['errors'].append(info)
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
            store.update(root, report['run_id'], report)
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


# 账号级调度入口：持锁、检查到期、按时间槽去重、读取配置，最后持久化脱敏运行结果。
def finish_run(root, run_id, report, outcome, notify=None):
    # 先提交游戏执行结果，再独立尝试邮件；邮件失败不能导致再次提交游戏请求。
    store.update(root, run_id, report, outcome)
    if notify is not None:
        try:
            notify(run_id, report, outcome)
        except Exception:
            print('Email notification unavailable; game execution result retained.', flush=True)


def execute_user(root, helper, check_only=False, run_now=False, notify=None, account=None):
    import auth
    lock = auth.operation_lock(*account) if account else OperationLock(root)
    if not lock.acquire():
        if account:
            # 可能正被删除，此时不能绕过锁访问或创建账号数据库。
            print('SKIP: another account operation is running.', flush=True)
            return 0
        at = store.now()
        if store.due(store.settings(root), at):
            with store.connect(root) as db:
                db.execute("INSERT OR IGNORE INTO runs(slot,started,finished,outcome,details) VALUES (?,?,?,'skipped',?)",
                           (at.strftime('%Y-%m-%dT%H:%M'), at.isoformat(), at.isoformat(), json.dumps({'message':'其他操作正在执行，本次已跳过，避免重复提交。'})))
        print('SKIP: another operation is running.', flush=True)
        return 0
    run_id = None
    report = {'check_only': check_only, 'trigger': 'scheduled_now' if run_now else 'scheduled'}
    try:
        if account and (not auth.user_exists(*account) or not root.is_dir()):
            return 0
        at = store.now()
        schedule = store.settings(root)
        if not check_only and (not schedule['enabled'] or (not run_now and not store.due(schedule, at))):
            return 0
        config_path = root / 'config-dashboard.json'
        if check_only and not config_path.exists():
            return 0
        slot = at.strftime('%Y-%m-%dT%H:%M') if not check_only and not run_now else None
        run_id = store.begin(root, slot)
        if run_id is None:
            return 0
        report['run_id'] = run_id
        if not config_path.exists():
            report['message'] = '未导入面板账号，已跳过。'
            store.update(root, run_id, report, 'skipped')
            return 0
        config = json.loads(config_path.read_text(encoding='utf-8'))
        code = run(helper, config, root, check_only, report)
        finish_run(root, run_id, report, 'success' if code == 0 else 'error', notify)
        return code
    except Exception as exc:
        error = error_info(exc, report.get('stage', '读取账号配置'))
        report.setdefault('errors', []).append(error)
        report['message'] = '；'.join(e['summary'] for e in report['errors'])
        if run_id:
            finish_run(root, run_id, report, 'error', notify)
        print('ERROR: query/write failed; see private execution history. No immediate retry.', flush=True)
        return 1
    finally:
        lock.release()


def main():
    import argparse
    import helper
    import auth
    import notifications
    from concurrent.futures import ThreadPoolExecutor
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--run-now', action='store_true', help='Run enabled schedules once now')
    args = parser.parse_args()
    root = Path(os.environ.get('ARK_HELPER_DIR', '/var/lib/ark-dashboard'))
    os.umask(0o077)
    auth.initialize(root)
    users = auth.all_users(root)
    accounts = [(user, auth.user_root(root, user, create=False)) for user in users]
    # 在账号锁内判断到期和账号是否仍存在，避免列表快照中的已删除账号继续执行。
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda pair: execute_user(pair[1], helper, args.check_only, args.run_now,
                            notifications.callback(root, pair[0], pair[1]), account=(root, pair[0])), accounts))
    return 1 if any(code for code in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())

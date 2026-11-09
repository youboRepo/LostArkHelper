"""QQ 邮箱异常提醒。SMTP 凭据仅存于私有状态目录，不进入 API 响应或任务日志。"""
from email.message import EmailMessage
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import tempfile

import auth
import store

MAIL_FILE = 'mail-settings.json'
DEFAULT = {'enabled': False, 'sender': '', 'authorization_code': ''}


def email_address(value):
    # 拒绝显示名称、多个收件人和换行；一次通知只发给当前账号的单个邮箱。
    if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", value):
        raise ValueError('请填写一个有效的收件邮箱地址。')
    return value


def load_mail(root):
    file = Path(root) / MAIL_FILE
    if not file.exists():
        return dict(DEFAULT)
    data = json.loads(file.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != set(DEFAULT) or type(data['enabled']) is not bool:
        raise ValueError('邮件配置无效。')
    if data['sender']:
        email_address(data['sender'])
        if data['sender'].rsplit('@', 1)[1].lower() not in ('qq.com', 'vip.qq.com'):
            raise ValueError('发件邮箱须使用 QQ 邮箱。')
    if not isinstance(data['authorization_code'], str) or (data['authorization_code'] and
            not re.fullmatch(r'[A-Za-z0-9]{16,128}', data['authorization_code'])):
        raise ValueError('请填写 QQ 邮箱的 SMTP 授权码。')
    return data


def public_mail(root):
    data = load_mail(root)
    return {key: data[key] for key in ('enabled', 'sender')} | {
        'has_authorization_code': bool(data['authorization_code']),
        'ready': bool(data['enabled'] and data['sender'] and data['authorization_code'])}


def save_mail(root, value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT) or type(value['enabled']) is not bool:
        raise ValueError('邮件配置字段无效。')
    previous = load_mail(root)
    sender = value['sender'].strip() if isinstance(value['sender'], str) else ''
    email_address(sender)
    if sender.rsplit('@', 1)[1].lower() not in ('qq.com', 'vip.qq.com'):
        raise ValueError('发件邮箱须使用 QQ 邮箱。')
    secret = value['authorization_code']
    if not secret and sender == previous['sender']:
        secret = previous['authorization_code']
    if not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9]{16,128}', secret):
        raise ValueError('请填写 QQ 邮箱的 SMTP 授权码（不是邮箱登录密码）。')
    data = dict(enabled=value['enabled'], sender=sender, authorization_code=secret)
    # NamedTemporaryFile 默认 0600；原子替换，避免读到半份配置，也不依赖进程 umask。
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=root, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temp, Path(root) / MAIL_FILE)
        finally:
            temp.unlink(missing_ok=True)
    return public_mail(root)


def validate_preferences(value):
    if not isinstance(value, dict) or set(value) != {'email', 'enabled'} or type(value['enabled']) is not bool:
        raise ValueError('邮件提醒设置无效。')
    email = value['email'].strip() if isinstance(value['email'], str) else ''
    if email or value['enabled']:
        email_address(email)
    return {'email': email, 'enabled': value['enabled']}


def build_message(sender, recipient, user, run_id, report):
    # 不拼接接口响应、活动链接、任务错误原文；只使用固定原因说明，避免邮件携带 token。
    reasons = {
        'auth_invalid': 'QQ 登录凭据已过期或失效，请在账号管理更新 token。',
        'auth_mismatch': 'QQ 登录凭据与账号不一致，请重新获取当前角色的活动链接。',
        'auth_check': 'QQ 登录校验暂不可用，请稍后重试。',
        'unconfirmed': '上次签到或领奖结果未确认，请核对官方状态并更新 token。',
        'business': '官方活动接口拒绝操作，请在工作台查看执行记录。',
        'outer_response': '官方活动接口返回异常，请在工作台查看执行记录。',
        'network': '连接官方活动接口失败，请检查网络后重试。',
        'timeout': '请求超时，操作结果可能尚未确认，请核对官方状态。',
    }
    kinds = [error.get('kind') for error in report.get('errors', []) if isinstance(error, dict)]
    reason = reasons.get(kinds[0] if kinds else '', '执行异常，请在工作台查看运行记录。')
    message = EmailMessage()
    message['From'], message['To'] = sender, recipient
    message['Subject'] = '命运方舟助手：任务执行异常'
    message.set_content('账号：{}\n执行记录：#{}\n通知时间：{}（北京时间）\n\n{}\n\n'
                        '请登录工作台查看详情。邮件不包含游戏登录凭据。\n'.format(
                            user['username'], int(run_id), store.now().strftime('%Y-%m-%d %H:%M:%S'), reason))
    return message


def notify_error(base, user, account_root, run_id, report, outcome):
    """一次失败执行最多尝试通知一次。邮件失败只记通知结果，不改变游戏执行结果。"""
    if outcome != 'error' or report.get('check_only'):
        return
    try:
        preference = auth.notification_preferences(base, user)
        mail = load_mail(base)
        if not preference['enabled']:
            return
        ready = mail['enabled'] and mail['sender'] and mail['authorization_code']
        # 发送前提交占位，防止并发或同一次执行重复完成时反复发信。
        with store.connect(account_root) as db:
            cursor = db.execute('INSERT OR IGNORE INTO mail_deliveries VALUES (?,?,?)',
                                (run_id, 'sending' if ready else 'not_configured', '' if ready else '管理员尚未启用 QQ 发信。'))
            if not cursor.rowcount:
                return
        if not ready:
            return
        recipient = email_address(preference['email'])
        state, reason = 'sent', ''
        try:
            message = build_message(mail['sender'], recipient, user, run_id, report)
            # 固定 QQ SMTP SSL 端口，验证证书，不接受浏览器指定任意 SMTP 主机。
            with smtplib.SMTP_SSL('smtp.qq.com', 465, context=ssl.create_default_context(), timeout=10) as smtp:
                smtp.login(mail['sender'], mail['authorization_code'])
                refused = smtp.send_message(message)
                if refused:
                    state, reason = 'failed', '收件邮箱被邮件服务器拒绝。'
        except smtplib.SMTPAuthenticationError:
            state, reason = 'failed', 'QQ 邮箱认证失败，请联系管理员检查 SMTP 授权码。'
        except Exception:
            state, reason = 'failed', '邮件未能确认发送，请检查 QQ 邮箱配置或网络。'
        with store.connect(account_root) as db:
            db.execute('UPDATE mail_deliveries SET state=?,reason=? WHERE run_id=?', (state, reason, run_id))
    except Exception:
        # 不能让 SMTP/磁盘异常使游戏任务被改写成失败或再执行一次。
        print('Email notification unavailable; game execution result retained.', flush=True)


def callback(base, user, account_root):
    return lambda run_id, report, outcome: notify_error(base, user, account_root, run_id, report, outcome)

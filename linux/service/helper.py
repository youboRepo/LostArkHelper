#!/usr/bin/env python3
"""方舟最佳搭档任务助手：默认只查询，领取须显式启用。"""
import argparse
import hashlib
import time
import socket
import re
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'https://api.bbs.lostark.qq.com/'
# Version and mutation whitelist must be reviewed together after each activity change.
ACTIVITY_PATH = (Path(sys._MEIPASS) / 'service' / 'activity.json') if getattr(sys, 'frozen', False) else Path(__file__).with_name('activity.json')
ACTIVITY = json.loads(ACTIVITY_PATH.read_text(encoding='utf-8'))
VERSION = ACTIVITY['version']
if not isinstance(VERSION, str) or not re.fullmatch(r'[0-9]{4}', VERSION):
    raise ValueError('Invalid activity version')
LIST = 'partnergetusertask' + VERSION
CLAIM = 'partnergettaskreward' + VERSION
SIGN_LIST = 'partnersignlist' + VERSION
SIGN_IN = 'partnersignin' + VERSION
VERIFIED = {int(key): value for key, value in ACTIVITY['verified_claims'].items()}
if any(key <= 0 or type(value) is not int or value != 1 for key, value in VERIFIED.items()):
    raise ValueError('Invalid verified claim mapping')
TYPES = {1: '每日', 2: '每周', 3: '活动'}
STATUS = {0: '未完成（推测）', 1: '可领取（推测）', 2: '已完成/已领取（推测）', 3: '已结束（样本）'}

class ApiError(Exception):
    def __init__(self, message, kind='api', endpoint=None, code=None):
        super().__init__(message)
        self.kind, self.endpoint, self.code = kind, endpoint, code

# 将服务端错误提示中的凭据和疑似长令牌遮盖后再展示，不能直接输出原始响应。
def safe_message(data, config):
    text = next((data[k] for k in ('message', 'msg', 'errmsg') if isinstance(data.get(k), str)), '')
    for value in [config.get('cookie', '')] + list(config.get('form', {}).values()):
        if isinstance(value, str) and len(value) >= 4:
            text = text.replace(value, '[redacted]')
    text = re.sub(r'https?://\S+|[A-Za-z0-9_./+=%-]{12,}', '[redacted]', text)
    return ''.join(c for c in text if c.isprintable())[:160]


# QQ 登录过期时，活动任务接口可能仍返回 code=200、status=0，不能据此显示未完成。
# 先用 QQ 官方 OAuth 身份查询校验 access_token；成功结果按凭据摘要短暂缓存。
_LOGIN_CACHE = {}
LOGIN_CACHE_SECONDS = 60


def validate_login(config, force=False):
    form = config.get('form', {})
    access, appid, openid = (form.get(k) for k in ('access_token', 'appid', 'openid'))
    if any(not isinstance(v, str) or not v for v in (access, appid, openid)):
        raise ApiError('QQ 登录凭据不完整，请在账号管理更新 token。', 'auth_invalid', 'qq_oauth_me')
    key = hashlib.sha256(json.dumps([access, appid, openid]).encode()).hexdigest()
    now = time.monotonic()
    if not force and _LOGIN_CACHE.get(key, 0) > now:
        return
    # 不向 OAuth 主机发送活动 Cookie；固定 HTTPS 主机并拒绝重定向。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    url = 'https://graph.qq.com/oauth2.0/me?' + urllib.parse.urlencode(
        {'access_token': access, 'fmt': 'json'})
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    try:
        opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as response:
            raw = response.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        text = raw.decode('utf-8').strip()
        callback = re.fullmatch(r'callback\s*\(\s*(\{.*\})\s*\)\s*;?', text, re.S)
        data = json.loads(callback.group(1) if callback else text)
        if not isinstance(data, dict):
            raise ValueError()
    except (OSError, urllib.error.URLError, ValueError):
        _LOGIN_CACHE.pop(key, None)
        raise ApiError('QQ 登录校验暂时不可用，请稍后重试；本次停止查询和提交。',
                       'auth_check', 'qq_oauth_me') from None
    if 'error' in data or data.get('ret', 0) != 0:
        _LOGIN_CACHE.pop(key, None)
        code = data.get('error', data.get('ret'))
        code = code if type(code) is int else None
        raise ApiError('QQ 登录凭据已过期或失效，请从官方 App 获取新活动链接，在账号管理更新 token。',
                       'auth_invalid', 'qq_oauth_me', code)
    if any(not isinstance(data.get(k), str) or not data[k] for k in ('client_id', 'openid')):
        _LOGIN_CACHE.pop(key, None)
        raise ApiError('QQ 登录校验返回格式异常，请稍后重试；本次停止查询和提交。',
                       'auth_check', 'qq_oauth_me')
    if data.get('client_id') != appid or data.get('openid') != openid:
        _LOGIN_CACHE.pop(key, None)
        raise ApiError('QQ 登录凭据与链接中的应用或账号不一致，请重新获取完整链接。',
                       'auth_mismatch', 'qq_oauth_me')
    if len(_LOGIN_CACHE) >= 256:
        _LOGIN_CACHE.clear()
    _LOGIN_CACHE[key] = time.monotonic() + LOGIN_CACHE_SECONDS


# 官方 API 的唯一请求出口。合并通用凭据与本次参数，编码为表单；拒绝重定向，验证外层和业务层返回码。
def request(config, endpoint, extra=None):
    validate_login(config)
    form = dict(config['form'])
    form.update(extra or {})
    body = urllib.parse.urlencode(form).encode('utf-8')
    req = urllib.request.Request(BASE + endpoint, data=body, headers={
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Origin': 'https://bbs.lostark.qq.com',
        'Referer': 'https://bbs.lostark.qq.com/',
        'User-Agent': config.get('user_agent', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 GH_QQConnect GameHelper_1010/1.10.0.4.2103160004'),
        'Cookie': config['cookie'],
    }, method='POST')
    try:
        # Do not follow redirects: never forward authentication data to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ApiError('接口 HTTP 错误：{}'.format(exc.code), 'http', endpoint, exc.code) from None
    except (TimeoutError, socket.timeout):
        raise ApiError('请求超时，服务端处理结果未知', 'timeout', endpoint) from None
    except urllib.error.URLError as exc:
        kind = 'timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'network'
        raise ApiError('请求超时' if kind == 'timeout' else '网络连接失败（DNS、TLS 或连接异常）', kind, endpoint) from None
    except OSError:
        raise ApiError('网络连接中断', 'network', endpoint) from None
    except ValueError:
        raise ApiError('接口返回的内容不是有效 JSON', 'invalid_json', endpoint) from None
    if not isinstance(data, dict) or not isinstance(data.get('data'), dict) or data.get('code') != 200:
        code = data.get('code') if isinstance(data, dict) else None
        code = code if type(code) in (int, float) else None
        detail = safe_message(data, config) if isinstance(data, dict) else ''
        raise ApiError('服务端外层响应异常' + (': ' + detail if detail else ''), 'outer_response', endpoint, code)
    inner = data['data']
    if inner.get('code') != 0:
        code = inner.get('code') if type(inner.get('code')) in (int, float) else None
        detail = safe_message(inner, config)
        raise ApiError('业务接口拒绝操作' + (': ' + detail if detail else ''), 'business', endpoint, code)
    return inner

# 验证任务列表结构，确保 ID、分类和状态是整数；任务分类 type 不等于领奖参数 type。
def tasks(config):
    result = request(config, LIST).get('data')
    if not isinstance(result, list):
        raise ApiError('任务列表格式不符合预期，停止执行')
    for task in result:
        if not isinstance(task, dict) or any(type(task.get(k)) is not int for k in ('id', 'type', 'status')):
            raise ApiError('任务字段不符合预期，停止执行')
    return result

# 签到状态必须是布尔值 is_sign；不能把字符串 "false" 当作真假状态。
def sign_status(config):
    payload = request(config, SIGN_LIST).get('data')
    if not isinstance(payload, dict) or type(payload.get('is_sign')) is not bool:
        raise ApiError('签到状态格式不符合预期，已停止')
    return payload['is_sign']

# 仅在未签到时提交一次，之后重新查询确认；接口返回成功并不代替状态确认。
def sign_in(config):
    if sign_status(config):
        print('今天已签到，跳过。')
        return
    print('正在签到……', flush=True)
    try:
        request(config, SIGN_IN)
    except ApiError as exc:
        raise ApiError('签到结果未确认：' + str(exc) + '；不重试，请在 App 中核实') from exc
    try:
        signed = sign_status(config)
    except ApiError as exc:
        raise ApiError('签到接口报告成功，但查询确认失败；请在 App 中核实，不要盲目重试') from exc
    if not signed:
        raise ApiError('签到接口报告成功，但状态未变为已签到；请在 App 中核实，不要盲目重试')
    print('签到接口报告成功，且状态已确认为已签到。')

def show(items):
    for t in items:
        name = str(t.get('name', '未知任务')).replace('\n', ' ').replace('\r', ' ')
        print(f"[{TYPES.get(t['type'], '未知')}] ID={t['id']} {name} | {STATUS.get(t['status'], '未知状态')} (status={t['status']})")

def main():
    parser = argparse.ArgumentParser(description='查询方舟最佳搭档任务；默认不会领取')
    parser.add_argument('--config', default='config.json', help='凭据文件路径')
    parser.add_argument('--claim', action='store_true', help='领取当前可领取且在已验证白名单中的任务')
    parser.add_argument('--sign', action='store_true', help='仅在未签到时执行当日签到，不补签')
    args = parser.parse_args()
    try:
        p = Path(args.config)
        config = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(config, dict) or not isinstance(config.get('form'), dict) or not isinstance(config.get('cookie'), str) or not config['cookie']:
            raise ApiError('配置无效：需要 form 对象和非空 cookie')
        required = ('token', 'user_id', 'openid', 'access_token', 'inv_user_id', 'login_type', 'sRoleld', 'iUin', 'sGiftPartition', 'sGiftArea', 'acctype', 'appid')
        if any(k not in config['form'] or not isinstance(config['form'][k], str) or not config['form'][k] or config['form'][k].startswith('<') for k in required) or config['cookie'].startswith('<'):
            raise ApiError('配置不完整：请在本地填写所有表单字段和 Cookie，数值也请用字符串')
        if set(config['form']) != set(required):
            raise ApiError('form 含未知字段；请只保留示例中的字段')
        if os.name == 'posix' and p.stat().st_mode & 0o077:
            print('警告：配置文件可能可被其他用户读取，建议 chmod 600 配置文件。', file=sys.stderr)
        if args.sign:
            sign_in(config)
        items = tasks(config)
        show(items)
        if not args.claim:
            print('\n没有发送任务领取请求。' + ('本次允许签到。' if args.sign else '本次仅查询任务，未签到。'))
            return 0
        candidates = [t for t in items if t['status'] == 1 and t['id'] in VERIFIED]
        unknown = [t for t in items if t['status'] == 1 and t['id'] not in VERIFIED]
        if unknown:
            print('\n有可领取但未收录领取参数的任务，已跳过：' + ', '.join(str(t['id']) for t in unknown))
        if not candidates:
            print('没有可领取的已支持任务。')
            return 0
        for candidate in candidates:
            # Refresh before every mutation, ensuring it remains claimable.
            current = [t for t in tasks(config) if t['id'] == candidate['id']]
            if len(current) != 1 or current[0]['status'] != 1:
                print(f"ID={candidate['id']} 状态已变化或不唯一，跳过。")
                continue
            tid = candidate['id']
            print(f'领取 ID={tid} ...', flush=True)
            try:
                request(config, CLAIM, {'task_id': str(tid), 'type': str(VERIFIED[tid])})
            except ApiError as exc:
                print(f'领取未确认：{exc}。不重试，请检查 App 中的状态。', file=sys.stderr)
                return 1
            try:
                after = [t for t in tasks(config) if t['id'] == tid]
                if len(after) != 1 or after[0]['status'] != 2:
                    print(f'ID={tid} 接口报告成功，但状态未确认；停止，请在 App 中检查。', file=sys.stderr)
                    return 1
                print(f"ID={tid} 接口报告成功，领取后状态={after[0]['status']}。")
            except ApiError:
                print(f'ID={tid} 接口报告成功，但刷新状态失败；停止，请在 App 中检查。', file=sys.stderr)
                return 1
        return 0
    except (ApiError, OSError, ValueError, TypeError) as exc:
        print(f'错误：{exc if isinstance(exc, ApiError) else "配置文件无法读取或格式有误"}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Multi-user dashboard with private server-side game credentials."""
import json
import os
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, parse_qs
from automation import OperationLock, run, save_state, error_info
import store
import auth
import helper
import import_link as importer

ROOT = Path(os.environ.get('ARK_HELPER_DIR', '/var/lib/ark-dashboard'))
STATIC = Path(os.environ.get('ARK_STATIC_DIR', '/opt/lostark-dashboard/dist'))
PORT = int(os.environ.get('ARK_PORT', '18745'))
PUBLIC_ORIGIN = os.environ.get('ARK_PUBLIC_ORIGIN', 'https://lostarkhelper.dpdns.org').rstrip('/')
COOKIE = 'ark_session'

# 读取当前账号的游戏凭据；只供后端内部调用，不能把返回值直接输出到网页或日志。
def read_config(root):
    obj = json.loads((root / 'config-dashboard.json').read_text(encoding='utf-8'))
    if not isinstance(obj, dict) or not isinstance(obj.get('form'), dict) or not isinstance(obj.get('cookie'), str):
        raise ValueError('invalid')
    return obj

# 向官方接口查询签到和任务，向网页仅返回展示所需的字段；此函数不提交签到或领奖。
def status(root):
    if not (root / 'config-dashboard.json').exists():
        return {'configured': False, 'source': None}
    try:
        config = read_config(root)
        signed = helper.sign_status(config)
        tasks = helper.tasks(config)
        cleaned = [{'id': t['id'], 'name': str(t.get('name', '未知任务'))[:100],
                    'type': t['type'], 'status': t['status'], 'exp': t.get('exp')} for t in tasks]
        return {'configured': True, 'source': '我的游戏账号', 'signed': signed, 'tasks': cleaned,
                'time': datetime.now(store.TZ).strftime('%H:%M')}
    except Exception:
        return {'configured': True, 'source': '我的游戏账号',
                'error': '查询失败，请更新登录凭据或检查活动是否有效。'}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    # 统一添加缓存限制、CSP 和禁止嵌入等响应头，减少凭据页面被其他站点利用的机会。
    def common(self, content_type, length):
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()

    def reply(self, code, data, cookie=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        self.common('application/json; charset=utf-8', len(body))
        self.wfile.write(body)

    # 仅接受本机地址，防止浏览器通过其他 Host 名称绕过本机访问边界。
    def valid_host(self):
        return self.headers.get('Host', '') in (f'localhost:{PORT}', f'127.0.0.1:{PORT}')

    def session_token(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            return cookies[COOKIE].value if COOKIE in cookies else ''
        except Exception:
            return ''

    # 生成会话 Cookie；HttpOnly 阻止页面脚本读取，SameSite=Strict 限制跨站携带。
    def cookie(self, value, expires=False):
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        return '{}={}; Path=/; HttpOnly; SameSite=Strict; Max-Age={}{}'.format(
            COOKIE, value, 0 if expires else auth.SESSION_SECONDS, secure)

    # 根据 HttpOnly 会话 Cookie 确定当前访问身份；服务器版与本地版的身份来源不同。
    def current_user(self):
        return auth.session_user(ROOT, self.session_token())

    def do_GET(self):
        try:
            self.get()
        except Exception:
            self.reply(500, {'error': '服务暂时不可用，请稍后重试。'})

    # GET 路由：处理状态、计划、历史等查询，以及构建后的前端静态文件。
    def get(self):
        if not self.valid_host():
            return self.reply(403, {'error': '访问地址不允许'})
        path = urlsplit(self.path).path
        if path.startswith('/api/'):
            user = self.current_user()
            if path == '/api/auth/me':
                return self.reply(200, {'user': {'username': user['username']} if user else None})
            if not user:
                return self.reply(401, {'error': '请先登录。'})
            root = auth.user_root(ROOT, user)
            if path == '/api/schedule':
                value = store.settings(root)
                return self.reply(200, dict(value, timezone='Asia/Shanghai', next_run=store.next_run(value)))
            if path == '/api/history':
                try:
                    before = int(parse_qs(urlsplit(self.path).query).get('before', ['0'])[0])
                    if before < 0: raise ValueError()
                except ValueError:
                    return self.reply(400, {'error': '分页参数无效'})
                return self.reply(200, store.history(root, before))
            if path == '/api/status':
                return self.reply(200, status(root))
            return self.reply(404, {'error': 'not found'})
        if path in ('/', '/login', '/register'):
            path = '/index.html'
        if '..' in path or not (path == '/index.html' or path.startswith('/assets/')):
            return self.reply(404, {'error': 'not found'})
        file = (STATIC / path.lstrip('/')).resolve()
        if not file.is_relative_to(STATIC.resolve()) or not file.is_file():
            return self.reply(404, {'error': 'not found'})
        typ = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
               '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml', '.woff2': 'font/woff2'}.get(file.suffix, 'application/octet-stream')
        body = file.read_bytes()
        self.send_response(200)
        self.common(typ, len(body))
        self.wfile.write(body)

    def do_POST(self):
        try:
            self.post()
        except Exception:
            self.reply(500, {'error': '操作未能确认，请检查运行记录；不要盲目重试。'})

    # POST 路由：先验证 Host、Origin、自定义头及 JSON 大小，再处理配置和写操作。
    def post(self):
        origin = self.headers.get('Origin', '')
        local = origin == 'http://' + self.headers.get('Host', '')
        public = origin == PUBLIC_ORIGIN and self.headers.get('X-Forwarded-Proto') == 'https'
        if not self.valid_host() or not (local or public) or self.headers.get('X-Ark-Request') != '1' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(403, {'error': '请求来源无效'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 1 <= size <= 16384: raise ValueError()
            obj = json.loads(self.rfile.read(size))
            if not isinstance(obj, dict): raise ValueError()
        except Exception:
            return self.reply(400, {'error': '请求格式无效'})
        path = urlsplit(self.path).path
        if path in ('/api/auth/login', '/api/auth/register'):
            username, password = obj.get('username'), obj.get('password')
            identity = self.headers.get('CF-Connecting-IP', self.client_address[0])
            if not auth.rate_limit(ROOT, identity, username, path.endswith('register')):
                return self.reply(429, {'error': '尝试过于频繁，请在 15 分钟后重试。'})
            if path.endswith('register'):
                try:
                    user = auth.create_user(ROOT, username, password)
                    root = auth.user_root(ROOT, user)
                    store.settings(root, dict(store.DEFAULT))
                except ValueError as exc:
                    return self.reply(400, {'error': str(exc)})
                # Registration does not create a logged-in session.
                return self.reply(201, {'message': '注册成功，请使用新账号登录。'})
            user = auth.authenticate(ROOT, username, password)
            if not user:
                return self.reply(401, {'error': '用户名或密码错误。'})
            token = auth.new_session(ROOT, user)
            return self.reply(200, {'user': {'username': user['username']}}, self.cookie(token))
        user = self.current_user()
        if not user:
            return self.reply(401, {'error': '请先登录。'})
        if path == '/api/auth/logout':
            auth.logout(ROOT, self.session_token())
            return self.reply(200, {'message': '已退出登录。'}, self.cookie('', expires=True))
        root = auth.user_root(ROOT, user)
        lock = OperationLock(root)
        if not lock.acquire():
            return self.reply(409, {'error': '你的账号已有操作正在进行，请稍后再试。'})
        try:
            if path == '/api/schedule':
                try:
                    value = store.settings(root, obj)
                except ValueError:
                    return self.reply(400, {'error': '时间设置无效，请检查分钟及 HH:MM 格式。'})
                return self.reply(200, dict(value, timezone='Asia/Shanghai', next_run=store.next_run(value), message='我的定时设置已保存。'))
            if path == '/api/import':
                if not isinstance(obj.get('url'), str):
                    return self.reply(400, {'error': '请填写分享链接。'})
                file = root / 'config-dashboard.json'
                if file.exists() and obj.get('replace') is not True:
                    return self.reply(409, {'error': '已有 token，请勾选确认更新后再提交。'})
                try:
                    config, info = importer.parse_link(obj['url'])
                except ValueError:
                    return self.reply(400, {'error': '分享链接无效，请从官方 App 复制支持的活动链接。'})
                if file.exists():
                    old = read_config(root)
                    if any(str(old['form'].get(k)) != str(config['form'].get(k)) for k in ('user_id', 'openid', 'sRoleld')):
                        return self.reply(409, {'error': '仅支持更新同一游戏账号和角色的 token，不能在此切换账号。'})
                save_state(file, config)
                # 已持有该账号的操作锁，且凭据保存成功；允许用新凭据重新查询后执行。
                # 校验失败、身份不符或写入失败时不会执行到这里，原待确认记录仍保留。
                pending = root / 'pending-operation.json'
                recovered = pending.exists()
                pending.unlink(missing_ok=True)
                names = ' / '.join(str(info.get(k) or '未提供')[:100] for k in ('area', 'server', 'role'))
                return self.reply(200, {'message': '我的 token 已保存：' + names + ('。待确认限制已解除，下次执行将重新查询签到和任务状态。' if recovered else '。请先查询核对，再启用定时任务。'), 'pending_cleared': recovered})
            if path not in ('/api/sign', '/api/claim', '/api/run'):
                return self.reply(404, {'error': 'not found'})
            if not (root / 'config-dashboard.json').exists():
                return self.reply(400, {'error': '请先导入自己的游戏账号。'})
            report = {'trigger': 'manual'}
            run_id = store.begin(root, None)
            report['run_id'] = run_id
            try:
                code = run(helper, read_config(root), root, report=report,
                           allow_sign=path in ('/api/sign', '/api/run'), allow_claim=path in ('/api/claim', '/api/run'))
                store.update(root, run_id, report, 'success' if code == 0 else 'error')
                return self.reply(200 if code == 0 else 409, {'message': ('执行成功：' + ('本次签到成功' if report.get('sign') == 'signed_now' else '今日已签到，跳过签到') + '；领取奖励 {} 项。'.format(len(report.get('claimed', [])))) if code == 0 else report.get('message', '执行失败，请查看运行记录。')})
            except Exception as exc:
                report.setdefault('errors', []).append(error_info(exc, report.get('stage', '手动操作')))
                report['message'] = '；'.join(e['summary'] for e in report['errors'])
                store.update(root, run_id, report, 'error')
                return self.reply(500, {'error': report['message']})
        finally:
            lock.release()

if __name__ == '__main__':
    os.umask(0o077)
    auth.initialize(ROOT)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.timeout = 30
    server.serve_forever()

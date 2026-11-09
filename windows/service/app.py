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
import credentials
import autostart
import hmac
import import_link as importer
import updater

ROOT = Path(os.environ.get('ARK_HELPER_DIR', '/var/lib/ark-dashboard'))
STATIC = Path(os.environ.get('ARK_STATIC_DIR', '/opt/lostark-dashboard/dist'))
PORT = int(os.environ.get('ARK_PORT', '18745'))
LOCAL_TOKEN = secrets.token_urlsafe(32)
COOKIE = 'ark_session'
INSTANCE_ID = ''
REQUEST_STOP = None

# 读取当前账号的游戏凭据；只供后端内部调用，不能把返回值直接输出到网页或日志。
def read_config(root):
    obj = credentials.read(root)
    if not isinstance(obj, dict) or not isinstance(obj.get('form'), dict) or not isinstance(obj.get('cookie'), str):
        raise ValueError('invalid')
    return obj

# 向官方接口查询签到和任务，向网页仅返回展示所需的字段；此函数不提交签到或领奖。
def status(root):
    if not credentials.exists(root):
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
        token = self.session_token()
        return {'id': 1, 'username': '本机账号', 'legacy': 1} if token and hmac.compare_digest(token, LOCAL_TOKEN) else None

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
        if path == '/api/local/health':
            return self.reply(200, {'instance': INSTANCE_ID, 'mode': 'local'})
        if path == '/open':
            token = parse_qs(urlsplit(self.path).query).get('key', [''])[0]
            if not hmac.compare_digest(token, LOCAL_TOKEN):
                return self.reply(403, {'error': '本机访问令牌无效，请使用启动器打开。'})
            self.send_response(303)
            self.send_header('Location', '/')
            self.send_header('Set-Cookie', self.cookie(LOCAL_TOKEN))
            self.common('text/plain', 0)
            return
        if path.startswith('/api/'):
            user = self.current_user()
            if path == '/api/auth/me':
                return self.reply(200, {'user': {'username': user['username']} if user else None})
            if not user:
                return self.reply(401, {'error': '请先登录。'})
            root = auth.user_root(ROOT, user)
            if path == '/api/local/autostart':
                try:
                    return self.reply(200, autostart.state())
                except OSError:
                    return self.reply(500, {'error': '无法读取 Windows 自启设置，请检查当前用户的权限。'})
            if path == '/api/local/update':
                return self.reply(200, updater.state())
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
        if not self.valid_host() or not local or self.headers.get('X-Ark-Request') != '1' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
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
            return self.reply(403, {'error': '本地版请使用启动器打开，无需注册或密码登录。'})
        user = self.current_user()
        if not user:
            return self.reply(401, {'error': '请先登录。'})
        if path == '/api/auth/logout':
            return self.reply(409, {'error': '本地版通过启动器访问，无需退出账号；停止服务请在运行窗口按 Ctrl+C。'})
        if path == '/api/local/stop':
            if obj != {'confirm': True}:
                return self.reply(400, {'error': '请确认停止本地服务。'})
            if REQUEST_STOP is None:
                return self.reply(503, {'error': '当前运行方式不支持从页面停止。'})
            self.reply(200, {'message': '正在停止服务，并等待已开始的操作完成。再次双击启动器可重新打开。'})
            REQUEST_STOP()
            return
        if path == '/api/local/update':
            try:
                if obj == {'check': True}:
                    updater.start()
                    return self.reply(202, dict(updater.state(), message='正在检查 GitHub 最新版本。'))
                if obj == {'install': True}:
                    updater.start(install=True)
                    return self.reply(202, dict(updater.state(), message='正在下载更新，请勿关闭电脑。'))
                return self.reply(400, {'error': '请选择检查或安装更新。'})
            except (RuntimeError, ValueError) as exc:
                return self.reply(409, {'error': updater.error_result(exc)})
        if path == '/api/local/autostart':
            if set(obj) != {'enabled'} or type(obj['enabled']) is not bool:
                return self.reply(400, {'error': '自启设置无效。'})
            try:
                result = autostart.set_enabled(obj['enabled'])
            except OSError:
                return self.reply(500, {'error': '无法修改 Windows 自启设置，请检查权限或系统安全软件。'})
            return self.reply(200, dict(result, message='已开启登录 Windows 后自动启动。' if obj['enabled'] else '已关闭开机自启；当前服务继续运行。'))
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
                configured = credentials.exists(root)
                if configured and obj.get('replace') is not True:
                    return self.reply(409, {'error': '已有 token，请勾选确认更新后再提交。'})
                try:
                    config, info = importer.parse_link(obj['url'])
                except ValueError:
                    return self.reply(400, {'error': '分享链接无效，请从官方 App 复制支持的活动链接。'})
                if configured:
                    old = read_config(root)
                    if any(str(old['form'].get(k)) != str(config['form'].get(k)) for k in ('user_id', 'openid', 'sRoleld')):
                        return self.reply(409, {'error': '仅支持更新同一游戏账号和角色的 token，不能在此切换账号。'})
                credentials.write(root, config)
                # 已持有该账号的操作锁，且凭据保存成功；允许用新凭据重新查询后执行。
                # 校验失败、身份不符或写入失败时不会执行到这里，原待确认记录仍保留。
                pending = root / 'pending-operation.json'
                recovered = pending.exists()
                pending.unlink(missing_ok=True)
                names = ' / '.join(str(info.get(k) or '未提供')[:100] for k in ('area', 'server', 'role'))
                return self.reply(200, {'message': '我的 token 已保存：' + names + ('。待确认限制已解除，下次执行将重新查询签到和任务状态。' if recovered else '。请先查询核对，再启用定时任务。'), 'pending_cleared': recovered})
            if path not in ('/api/sign', '/api/claim', '/api/run'):
                return self.reply(404, {'error': 'not found'})
            if not credentials.exists(root):
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
    raise SystemExit('请运行 launcher.py 启动本地版。')

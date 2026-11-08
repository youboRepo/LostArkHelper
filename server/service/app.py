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
from concurrent.futures import ThreadPoolExecutor
from automation import OperationLock, manual_run
import store
import auth
import accounts
import helper
import import_link as importer

ROOT = Path(os.environ.get('ARK_HELPER_DIR', '/var/lib/ark-dashboard'))
STATIC = Path(os.environ.get('ARK_STATIC_DIR', '/opt/lostark-dashboard/dist'))
PORT = int(os.environ.get('ARK_PORT', '18745'))
PUBLIC_ORIGIN = os.environ.get('ARK_PUBLIC_ORIGIN', 'https://lostarkhelper.dpdns.org').rstrip('/')
COOKIE = 'ark_session'

# 向官方接口查询签到和任务，向网页仅返回展示所需的字段；此函数不提交签到或领奖。
def status(acct):
    try:
        config = accounts.read_config(acct['root'])
        signed = helper.sign_status(config)
        tasks = helper.tasks(config)
        cleaned = [{'id': t['id'], 'name': str(t.get('name', '未知任务'))[:100],
                    'type': t['type'], 'status': t['status'], 'exp': t.get('exp')} for t in tasks]
        return {'configured': True, 'account': acct['id'], 'source': acct['label'], 'signed': signed, 'tasks': cleaned,
                'time': datetime.now(store.TZ).strftime('%H:%M')}
    except Exception:
        return {'configured': True, 'account': acct['id'], 'source': acct['label'],
                'error': '查询失败，请更新登录凭据或检查活动是否有效。'}

# 账号列表只含展示信息，不含任何凭据。
def public_accounts(user_root):
    return [{k: a[k] for k in ('id', 'label', 'role', 'server', 'area')} for a in accounts.list_accounts(user_root)]

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
            query = parse_qs(urlsplit(self.path).query)
            if path == '/api/accounts':
                return self.reply(200, {'accounts': public_accounts(root), 'max': accounts.MAX_ACCOUNTS})
            if path == '/api/history':
                try:
                    before = int(query.get('before', ['0'])[0])
                    if before < 0: raise ValueError()
                except ValueError:
                    return self.reply(400, {'error': '分页参数无效'})
                wanted = query.get('account', [None])[0]
                if wanted is not None and not accounts.valid_id(wanted):
                    return self.reply(400, {'error': '账号编号无效'})
                return self.reply(200, store.history(root, before, wanted))
            if path == '/api/status':
                items = accounts.list_accounts(root)
                if not items:
                    return self.reply(200, {'configured': False, 'source': None})
                acct = accounts.get(root, query.get('account', [items[0]['id']])[0])
                if not acct:
                    return self.reply(404, {'error': '账号不存在'})
                return self.reply(200, status(acct))
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
        if path == '/api/schedule':
            try:
                value = store.settings(root, obj)
            except ValueError:
                return self.reply(400, {'error': '时间设置无效，请检查分钟及 HH:MM 格式。'})
            return self.reply(200, dict(value, timezone='Asia/Shanghai', next_run=store.next_run(value), message='我的定时设置已保存。'))
        if path in ('/api/import', '/api/accounts/add', '/api/accounts/remove', '/api/accounts/rename'):
            return self.manage(path, root, obj)
        if path not in ('/api/sign', '/api/claim', '/api/run'):
            return self.reply(404, {'error': 'not found'})
        items = accounts.list_accounts(root)
        if not items:
            return self.reply(400, {'error': '请先导入自己的游戏账号。'})
        wanted = obj.get('account', items[0]['id'])
        if wanted == 'all':
            targets = items
        else:
            acct = accounts.get(root, wanted)
            if not acct:
                return self.reply(404, {'error': '账号不存在'})
            targets = [acct]
        sign, claim = path in ('/api/sign', '/api/run'), path in ('/api/claim', '/api/run')
        # 各账号独立加锁、独立记录；多账号时并发执行（上限 4），互不影响。
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
            results = list(pool.map(lambda a: manual_run(root, a, helper, sign, claim, capacity_root=ROOT), targets))
        code = 200 if all(c == 200 for c, _ in results) else next(c for c, _ in results if c != 200)
        message = results[0][1] if len(results) == 1 else '\n'.join(
            '{}：{}'.format(a['label'], m) for a, (_, m) in zip(targets, results))
        return self.reply(code, {'message': message} if code == 200 else {'error': message})

    # 账号绑定管理：导入/更新 token、新增、移除、改名。凭据只写入对应账号目录。
    def manage(self, path, root, obj):
        admin = OperationLock(root, 'accounts.lock')
        if not admin.acquire():
            return self.reply(409, {'error': '账号管理操作正在进行，请稍后再试。'})
        try:
            if path in ('/api/accounts/remove', '/api/accounts/rename'):
                acct = accounts.get(root, obj.get('account'))
                if not acct:
                    return self.reply(404, {'error': '账号不存在'})
                if path.endswith('rename'):
                    accounts.write_meta(acct['root'], accounts.clean_label(obj.get('label'), acct['label']))
                    return self.reply(200, {'message': '账号名称已更新。'})
                lock = OperationLock(acct['root'])
                if not lock.acquire():
                    return self.reply(409, {'error': '该账号正在执行操作，请稍后再移除。'})
                try:
                    accounts.remove(root, acct['id'])
                except accounts.PendingOperation as exc:
                    return self.reply(409, {'error': str(exc)})
                finally:
                    lock.release()
                return self.reply(200, {'message': '已移除账号「{}」及其登录凭据；历史记录保留。'.format(acct['label'])})
            if not isinstance(obj.get('url'), str):
                return self.reply(400, {'error': '请填写分享链接。'})
            try:
                config, info = importer.parse_link(obj['url'])
            except ValueError:
                return self.reply(400, {'error': '分享链接无效，请从官方 App 复制支持的活动链接。'})
            names = ' / '.join(str(info.get(k) or '未提供')[:100] for k in ('area', 'server', 'role'))
            default_label = (info.get('role') or '').strip()[:30]
            if path == '/api/accounts/add':
                dup = accounts.find_duplicate(root, config)
                if dup:
                    if dup.get('quarantined'):
                        return self.reply(409, {'error': '该角色存在已隔离的待确认操作，请先由管理员核实。'})
                    return self.reply(409, {'error': '该游戏角色已绑定为「{}」，请在该账号上更新 token。'.format(dup['label'])})
                try:
                    aid = accounts.next_id(root)
                except ValueError as exc:
                    return self.reply(400, {'error': str(exc)})
                label = accounts.clean_label(obj.get('label'), default_label or '账号 ' + aid)
                accounts.save(root, aid, config, label, info)
                return self.reply(201, {'message': '已添加账号「{}」：{}。请先查询核对，再启用定时任务。'.format(label, names), 'account': aid})
            # /api/import：更新已有账号的 token（须确认且为同一角色），或首次导入账号 1。
            aid = obj.get('account', accounts.FIRST)
            if not accounts.valid_id(aid):
                return self.reply(400, {'error': '账号编号无效'})
            acct = accounts.get(root, aid)
            if not acct and aid != accounts.FIRST:
                return self.reply(404, {'error': '账号不存在'})
            lock = OperationLock(accounts.account_root(root, aid))
            if not lock.acquire():
                return self.reply(409, {'error': '该账号正在执行操作，请稍后再更新。'})
            try:
                if acct:
                    if obj.get('replace') is not True:
                        return self.reply(409, {'error': '已有 token，请勾选确认更新后再提交。'})
                    old = accounts.read_config(acct['root'])
                    if any(str(old['form'].get(k)) != str(config['form'].get(k)) for k in ('user_id', 'openid', 'sRoleld')):
                        return self.reply(409, {'error': '仅支持更新同一游戏账号和角色的 token，不能在此切换账号。'})
                    accounts.save(root, aid, config, acct['label'], info)
                else:
                    dup = accounts.find_duplicate(root, config)
                    if dup:
                        if dup.get('quarantined'):
                            return self.reply(409, {'error': '该角色存在已隔离的待确认操作，请先由管理员核实。'})
                        return self.reply(409, {'error': '该游戏角色已绑定为「{}」。'.format(dup['label'])})
                    # 旧导入入口仅允许全新用户创建账号 1，已发放编号不得重用。
                    if accounts.issued_floor(root) != 0:
                        return self.reply(409, {'error': '账号 1 已使用过，请通过添加账号导入。'})
                    if accounts.next_id(root) != accounts.FIRST:
                        raise ValueError('账号编号分配异常')
                    accounts.save(root, aid, config, default_label or '账号 ' + aid, info)
            finally:
                lock.release()
            return self.reply(200, {'message': '我的 token 已保存：' + names + '。请先查询核对，再启用定时任务。'})
        finally:
            admin.release()

if __name__ == '__main__':
    os.umask(0o077)
    auth.initialize(ROOT)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.timeout = 30
    server.serve_forever()

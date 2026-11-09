"""仅服务器本地可用的管理员授权工具；不提供公开的自助提权接口。"""
import argparse
from contextlib import closing
import os
from pathlib import Path
import auth


def main():
    parser = argparse.ArgumentParser(description='将一个已注册账号设为管理员')
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('ARK_HELPER_DIR', '/var/lib/ark-dashboard')))
    parser.add_argument('--admin', required=True, help='已有用户名，例如 youbosama')
    args = parser.parse_args()
    if not (args.root / 'users.sqlite3').is_file():
        parser.error('账号数据库不存在，请核对 --root；不会创建新数据库。')
    with closing(auth.connect(args.root)) as db, db:
        changed = db.execute('UPDATE users SET is_admin=1 WHERE username=?', (args.admin.lower(),)).rowcount
        if not changed:
            parser.error('账号不存在，请先注册。')
    print('管理员权限已保存，用户下次刷新会话信息即可看到入口。')


if __name__ == '__main__': main()

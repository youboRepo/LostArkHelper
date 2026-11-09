#!/usr/bin/env python3
"""从用户粘贴的分享链接提取活动配置，不访问链接。"""
import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ALLOWED = {'lostark.qq.com', 'bbs.lostark.qq.com'}
FIELDS = ('token', 'userId', 'appOpenid', 'appid', 'roleId', 'serverId', 'areaId', 'uin')

# 只在内存解析用户粘贴的官方 HTTPS 链接，不访问该网页；返回完整请求配置和可展示的角色信息。
def parse_link(link):
    u = urlsplit(link.strip())
    if u.scheme != 'https' or u.hostname not in ALLOWED or u.username or u.password or u.port not in (None, 443):
        raise ValueError('仅接受 lostark.qq.com 或 bbs.lostark.qq.com 的 HTTPS 分享链接')
    # 官方不同活动使用 accType/acctype 等不同大小写；仅统一参数名，凭据值原样保留。
    # 合并大小写别名后检查值是否一致，不能用后一个值静默覆盖冲突值。
    q = {}
    for key, values in parse_qs(u.query, keep_blank_values=True).items():
        q.setdefault(key.lower(), []).extend(values)
    # 官方链接可能重复携带同一字段；同值重复可合并，缺失、空值或冲突值拒绝。
    def one(name):
        values = q.get(name.lower(), [])
        if not values or len(set(values)) != 1 or not values[0].strip():
            raise ValueError('链接缺少参数或同名参数的值不一致：' + name)
        return values[0]
    v = {k: one(k) for k in FIELDS}
    if v['appid'] != '102037747':
        raise ValueError('appid 与本助手的 QQ 登录应用不一致')
    if one('acctype') != 'qc':
        raise ValueError('目前只支持已验证的 QQ 登录（acctype=qc）')
    access_values = [one(key) for key in ('accessToken', 'access_token') if key.lower() in q]
    if not access_values or len(set(access_values)) != 1:
        raise ValueError('accessToken 缺失或不同字段的值不一致')
    access = access_values[0]
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', value) for value in (v['token'], v['appOpenid'], access)):
        raise ValueError('登录字段包含意外字符，已停止')
    for key in ('userId', 'appid', 'roleId', 'serverId', 'areaId', 'uin'):
        if not re.fullmatch(r'[0-9]+', v[key]):
            raise ValueError('数字字段格式不符合预期：' + key)
    form = {'token':v['token'], 'user_id':v['userId'], 'openid':v['appOpenid'], 'access_token':access,
            'inv_user_id':'false', 'login_type':'qc', 'sRoleld':v['roleId'], 'iUin':v['uin'],
            'sGiftPartition':v['serverId'], 'sGiftArea':v['areaId'], 'acctype':'qc', 'appid':v['appid']}
    # 依据现有样本构造；未证明所有 Cookie 均必需。
    cookie = '; '.join(f'{k}={value}' for k,value in [
        ('access_token',access),('acctype','qc'),('appid',v['appid']),('openid',v['appOpenid']),
        ('accessToken',access),('appId',v['appid']),('appOpenId',v['appOpenid']),
        ('appOpenid',v['appOpenid']),('openId',v['appOpenid']),('uin','o'+v['uin'].zfill(10))])
    return {'cookie':cookie, 'form':form}, {'server':q.get('servername',[''])[0], 'area':q.get('areaname',[''])[0], 'role':q.get('rolename',[''])[0]}

def main():
    parser=argparse.ArgumentParser(description='从 QQ 登录后的分享链接创建新配置；不访问网络、不覆盖现有文件')
    parser.add_argument('--output',default='config-imported.json',help='新配置文件路径（必须不存在）')
    args=parser.parse_args()
    try:
        if not sys.stdin.isatty():
            raise ValueError('请在交互式终端粘贴，避免从管道或命令参数传入登录链接')
        link=getpass.getpass('请粘贴活动分享链接（输入不会显示），然后按回车：')
        config,info=parse_link(link)
        out=Path(args.output)
        fd=os.open(str(out),os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump(config,f,ensure_ascii=False,indent=2)
                f.write('\n')
        except BaseException:
            try: out.unlink()
            except OSError: pass
            raise
        print('已创建配置文件（权限 600）：'+str(out))
        if any(info.values()): print('请核对区服/角色：'+' / '.join(info[k] or '未提供' for k in ('area','server','role')))
        print('字段映射 serverId → sGiftPartition、areaId → sGiftArea 基于已有样本，使用前请核对。')
        print('下一步请先运行只查询模式，确认账号和角色；不要直接领取。')
        return 0
    except (ValueError,OSError,EOFError,KeyboardInterrupt) as e:
        print('导入失败：'+(str(e) if isinstance(e,ValueError) else ('文件已存在，未覆盖' if isinstance(e,FileExistsError) else '输入取消或文件无法创建')),file=sys.stderr)
        return 1
if __name__=='__main__': sys.exit(main())

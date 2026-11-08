# 命运方舟助手 · 服务器版 v0.3.1

多用户网页服务，账号、凭据、计划、运行历史按用户隔离。此包包含 Python 后端、已构建网页、前端源码、测试与 systemd 单元。部署机需 Python 3.9+；运行时只使用标准库。

## 部署

将本目录内容放入 /opt/lostark-dashboard；服务只监听 127.0.0.1:18745。服务单元使用 systemd `DynamicUser`，程序目录中的文件夹必须可遍历、普通文件必须可读取。Release 压缩包会统一写成目录 `0755`、文件 `0644`；若手工复制文件，部署后还要执行：

```bash
sudo find /opt/lostark-dashboard -type d -exec chmod 0755 {} +
sudo find /opt/lostark-dashboard -type f -exec chmod 0644 {} +
```

首次启动前在服务器本地创建私有初始化密码文件（不应覆盖已有 panel.key）：

```bash
sudo install -d -m 700 /var/lib/ark-dashboard
sudo sh -c 'umask 077; test -e /var/lib/ark-dashboard/panel.key || python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /var/lib/ark-dashboard/panel.key'
sudo cp deploy/lostark-dashboard.service deploy/ark-dashboard-hourly.service deploy/ark-dashboard-hourly.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lostark-dashboard.service ark-dashboard-hourly.timer
```

初始化 ark 账号使用 panel.key 中的随机密码，服务启动后请在本机读取并妥善保管。其他用户可通过网页注册。已有部署直接保留整个 /var/lib/ark-dashboard，禁止把状态目录放入源码或发行包。

公网访问需要自行配置 HTTPS 反向代理，并将请求 Host 转为 127.0.0.1:18745、X-Forwarded-Proto 设为 https。使用新域名时通过 `sudo systemctl edit lostark-dashboard.service` 添加：

```ini
[Service]
Environment=ARK_PUBLIC_ORIGIN=https://你的域名
```

然后 daemon-reload 并重启服务。默认域名兼容既有部署。后端还支持 ARK_HELPER_DIR、ARK_STATIC_DIR、ARK_PORT；修改端口需同步代理。服务使用 systemd DynamicUser/StateDirectory 管理状态目录权限，不需要手工分发云端用户数据。

## 多游戏账号

一个网站登录账号最多可绑定 10 个游戏账号（`service/accounts.py` 的 `MAX_ACCOUNTS`），在「账号管理」页面添加、更新 token、改名或移除。定时计划按网站用户设置，对该用户的全部游戏账号分别生效。

- 每个游戏账号独立保存凭据、独立的 `pending-operation.json` 和操作锁；某个账号结果未确认时只阻止它自己，不影响其他账号。
- 账号 1 沿用原位置（用户目录下的 `config-dashboard.json`），其余账号保存在 `用户目录/accounts/<编号>/`。旧部署升级后原账号即为账号 1，无需迁移；运行记录库会自动补 `account` 列，旧记录归属账号 1。
- 编号只由服务端生成且不复用；同一游戏角色（user_id + sRoleld）不能重复绑定，更新 token 仍要求同一角色。
- 定时任务及「执行全部账号」按账号并发执行（全局最多 4 个同时进行），每个账号各写一条运行记录。移除账号会删除其凭据，运行记录保留；账号有未确认的待确认记录（`pending-operation.json`）时拒绝移除。账号 1 被移除后其编号也不再复用（计数器 `accounts/.next` 记录已发放的最大编号）。
- 接口：`GET /api/accounts`；`GET /api/status?account=<id>`；`POST /api/accounts/add|remove|rename`；`POST /api/import|sign|claim|run` 可带 `account`（编号或 `all`，缺省为第一个账号，兼容旧调用）。

网页手动执行与定时进程通过同一 `ARK_HELPER_DIR` 下的 `execution-0.lock` 至 `execution-3.lock` 共享 4 个执行额度；等待额度时不会占用账号操作锁。运行中禁止删除这些锁文件。到期任务在入队时固定时间槽，排队跨分钟仍执行原时间槽，暂停/修改计划后会在出队时重新校验该时间槽；不补跑停机期间未入队的任务。排队账号被移除时，手动操作返回 409，定时执行记录跳过日志，不影响其他账号。旧库 schema 检查与迁移通过 SQLite 写事务串行化。

## 更新及执行行为

升级前备份代码与私有状态，暂停 timer，等待当前 hourly.service 执行完成，再替换代码、重启网页服务并恢复 timer。各账号的计划仍按北京时间执行。不要为测试手动触发真实签到/领奖。

活动2611支持任务1–13，6–11为推断参数：极焰沙漏、累计签到7/14/21/28天、首次查看规则。仅领取官方已完成(status=1)的任务，type=1；不会自动完成副本或规则查看条件。签到后刷新任务；每笔提交后确认status=2。

发生网络错误、超时或状态无法确认时立即停止，并保留用户目录中的 pending-operation.json。之后自动查询确认；仍不确定时禁止再次提交。管理员应先暂停该用户计划、在官方 App 核实，必要时停止服务并备份后处理待确认记录，禁止将删除记录当作常规重试。

```bash
python3 -m unittest discover -s tests -v
```

单元测试使用假接口，不操作真实游戏。部署后用本机 HTTP 检查网页和登录响应。不要把“测试通过”解释为6–11已逐项真实领奖验证。

多账号健壮性回归位于 `tests/test_hardening.py`：覆盖跨分钟排队和时间槽去重、并发旧库升级及失败关闭、移除排队账号、混合批量结果、网页与定时共享额度，以及 Linux 跨进程占用与退出释放。请在 Linux 解压后的源码目录先执行上述完整单元测试，再热更新；测试不需要真实凭据或启动生产服务。

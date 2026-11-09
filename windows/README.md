# 命运方舟助手 Windows 本地版 · v0.3.2

支持 Windows 10/11 x64，以“本地服务 + 浏览器工作台”运行。GitHub Release 提供完整 ZIP 压缩包，内含 Python 运行时和已构建网页，使用者无需安装 Python 或 Node.js。无需注册账号，游戏请求由本机直接发送到腾讯官方接口。

## 使用与升级

1. 将下载的 ZIP **完整解压**到固定本地目录，双击 `LostArkHelper.exe`。如果 exe 在子目录中，请保留整个目录结构及其 `_internal` 资源。
2. 工作台自动打开。首次在“账号管理”粘贴官方 App 的完整活动链接，先刷新并核对角色和状态。
3. 程序在后台运行，没有需要保持打开的终端；关闭网页不停止服务，再次双击会打开已有实例的工作台。
4. 停止服务：工作台“定时设置 → 启动设置 → 停止本地服务”。程序会等待已经开始的操作完成。
5. “登录 Windows 后自动启动”可在同一页面开启。移动程序目录、从源码版切换或更新 exe 路径后，请关闭再开启自启以更新记录。

升级前先安全停止旧服务，然后替换程序文件。同一 Windows 用户下数据目录不变，可以继续使用已有配置和历史。首次运行默认不开启计划；避免本地与服务器同时调度同一个游戏账号。

源码运行适用于开发者：安装 Python 3.11+ 后双击 `启动助手.vbs`，或执行 `py -3 launcher.py`；`调试启动.cmd` 会保留终端便于排查。发行 ZIP 中的 exe 不需要这些工具。

## 活动与任务范围

活动版本为 **2611**，接口为 `partnergettaskreward2611`，启用任务 ID **1–13**，领取表单均为 `task_id=<任务 ID>&type=1`。查询列表中的任务分类 `type` 与领取参数 `type` 不是同一个含义。

| ID | 任务 | 参数来源 |
| --- | --- | --- |
| 1 | 登录游戏 | 已收录接口 |
| 2 | 讨伐1次星辰护卫 | 已收录接口 |
| 3 | 通关1次卡杰洛斯副本 | 已收录接口 |
| 4 | 通关2次影子副本 | 已收录接口 |
| 5 | 通关5次混沌地牢或库尔赞前线 | 已收录接口 |
| 6 | 通关1次极焰的沙漏 | 按已有接口推断，未获成功抓包验证 |
| 7 | 累计签到7天 | 按已有接口推断，未获成功抓包验证 |
| 8 | 累计签到14天 | 按已有接口推断，未获成功抓包验证 |
| 9 | 累计签到21天 | 按已有接口推断，未获成功抓包验证 |
| 10 | 累计签到28天 | 按已有接口推断，未获成功抓包验证 |
| 11 | 首次查看活动规则 | 按已有接口推断，未获成功抓包验证 |
| 12 | 首次成功组建4人小队 | 已收录接口 |
| 13 | 小队登录游戏(4/4) | 已收录接口 |

ID 6–11 按用户要求启用，**尚未用真实账号验证其领奖参数**。程序只能领取官方返回为可领取的奖励，不能代替通关、累计签到或查看活动规则等完成任务条件。

`service/activity.json` 中 `verified_claims` 为兼容历史代码保留的启用列表名称；`inferred_task_ids` 明确标注推断部分，不能把历史字段名当作验证证据。

执行时先查询签到和任务；未签到时签到，任务仅在 `status=1` 且 ID 已收录时提交。每笔提交前重新查询，提交后必须确认为 `status=2`；重复 ID、未完成、已领取及未知 ID 均跳过。超时、返回错误或查询未确认时停止后续操作，保存待确认记录，后续运行先核实状态，避免自动重试产生重复提交。

## 计划、数据和开机启动

- 支持每小时指定分钟或每天指定时间，均按北京时间 UTC+8。休眠、关机或未登录 Windows 时无法执行；恢复后只合并最近一次应执行计划，不逐小时补跑。
- 程序单实例，游戏写操作串行；计划和历史保存在 SQLite。
- 本地服务只监听 `127.0.0.1`，默认端口 18745；被占用时选择随机可用端口。浏览器通过启动器生成的 HttpOnly/SameSite Cookie 访问；每次重启需要重新打开工作台。
- 开机启动使用当前用户注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 的 `LostArkHelper` 项，不需要管理员权限。带 `--no-browser`，登录时不自动弹网页；操作系统启动应用设置或安全软件可能禁用启动项。

数据目录：`%LOCALAPPDATA%\LostArkHelper`。

| 文件 | 用途 |
| --- | --- |
| `credentials.dpapi` | 当前 Windows 用户 DPAPI 加密的游戏凭据，无明文回退 |
| `automation.sqlite3` | 定时设置和执行记录 |
| `pending-operation.json` | 未确认提交标记，无原始凭据 |
| `instance.json` | 本机服务会话及随机访问令牌，不要分享 |
| `service.log` | 脱敏启动日志，自动轮转 |

DPAPI 与当前 Windows 用户绑定，重装系统或换用户后可能需重新导入链接。同一已登录用户权限下的软件仍可能读取凭据，数据目录请勿公开共享。

提交未确认时，程序只查询核实，确认已签到或任务已领取后自动解除标记。若仍未确认，页面会提示“账号管理 → 更新我的 token”。成功保存同一游戏账号、同一角色的新链接后，自动解除待确认限制；下次执行先重新查询状态。导入或保存失败不会解除限制。更新 token 本身不会立即签到或领奖。

## 从源码构建

必须在 Windows 构建；推荐 Python 3.12 x64。构建脚本创建 `.build-venv`，安装锁定的 `PyInstaller==6.16.0`，运行测试，再运行实际冻结程序的冒烟测试。应用运行时代码使用 Python 标准库及 Windows DPAPI，开发者不需要额外 Python 包；网页资源已附在 `dist/`。

在仓库根目录执行目录构建：

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\build-windows.ps1 -Mode onedir
```

输出 `windows\release\LostArkHelper\LostArkHelper.exe`，发布时将整个 `LostArkHelper` 文件夹、使用说明和校验值压缩为 ZIP，不能只拷贝 exe。此模式将运行库放在同目录的 `_internal` 中。

默认不传 `-Mode` 时为单文件构建，输出 `windows\release\LostArkHelper.exe`，运行时将内置资源释放到临时目录。两种构建均不显示控制台，均须通过脚本末尾的实际 exe 检查。可以用 `-PythonExecutable C:\Path\python.exe` 指定构建用 Python。

修改网页时，再进入 `web/` 安装 Node 依赖并执行生产构建；仅运行或打包已有页面无需 Node.js。

## 验证范围

```powershell
py -3 -m unittest discover -s windows/tests -v
```

测试覆盖全部任务配置、每个推断任务的参数和前后状态确认、不完整/重复/未知任务跳过、超时后停止和重启阻断、锁互斥、SQLite 关闭与回滚、计划去重、HTTP 鉴权和安全停止、DPAPI 及自启注册表逻辑。Windows DPAPI 测试只在真实 Windows 执行。

`tests/smoke_frozen.py` 在 Windows 上用隔离的 PATH 和临时数据目录启动 exe，检查网页资源、鉴权、重复实例及安全退出，不调用游戏 API。浏览器自动弹出、用户桌面登录自启、休眠恢复以及 ID 6–11 真实领奖仍需对应设备和账号验证，模拟测试不能替代这些结果。

## 代码入口

- `launcher.py`：单实例、HTTP 服务、浏览器入口和调度线程。
- `background.pyw`：无控制台入口与轮转日志。
- `service/app.py`：本机网页 API 和安全停止。
- `service/credentials.py`：Windows DPAPI 凭据读写。
- `service/automation.py`：逐笔提交、状态确认和持久化待确认标记。
- `service/scheduler.py`、`store.py`：计划、最近时间槽和执行历史。
- `service/activity.json`、`helper.py`：活动范围、推断标记和官方接口。
- `service/autostart.py`、`platform_lock.py`：自启和平台锁。
- `web/`：前端源代码；`dist/`：已构建网页。

新增接口时需同步服务器、Windows、Linux 和 iPhone 快捷指令的适用配置。

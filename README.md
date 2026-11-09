# 命运方舟助手

v0.3.4，活动版本 2611。包含服务器、Windows 本地版、Linux 本地版及 iPhone 单快捷指令。Windows/Linux 解压即可运行，无需安装 Python，也无需注册助手账号；服务器版保留多用户登录和独立计划。

完整操作说明见：[命运方舟助手四端使用指南](docs/使用指南.md)。其中包含四端选择、活动链接导入、定时设置、数据位置、更新和安全排查。

从 [GitHub Releases](https://github.com/youboRepo/LostArkHelper/releases) 下载：

| 版本 | 下载文件 | 使用方式 |
| --- | --- | --- |
| Windows x64 | LostArkHelper-Windows-x64-v0.3.4.zip | 完整解压，双击目录中的 LostArkHelper.exe |
| Linux x64 | LostArkHelper-Linux-x64-v0.3.4.tar.gz | 解压，运行 ./start.sh，浏览器打开工作台 |
| 服务器 | LostArkHelper-Server-v0.3.4.tar.gz | Python 服务源码、网页成品及 systemd 单元，见包内 README |
| iPhone | LostArkHelper-iPhone-v0.3.4.shortcut | 在 iPhone 上下载并用快捷指令 App 导入 |

Windows/Linux 服务保持后台运行；关闭网页不退出，可在工作台停止。Windows 在工作台设置用户登录自启；Linux 使用安装脚本配置 systemd 用户服务。Linux 成品在 Ubuntu 22.04 x64 构建，需要 glibc 2.35+，不适用于 Alpine/musl 或 ARM。

## 工作台一键更新

Windows/Linux 打包版进入“定时设置 → 版本更新”，点击“检查更新”。发现新版后点击“下载并更新”，确认后服务会下载正式 Release、校验压缩包、退出旧进程、替换程序并自动重启。只发布到 GitHub 分支但未生成 Release 的代码不会被安装。

更新只替换程序目录，不替换用户目录里的游戏凭据、定时计划和历史记录。旧程序目录保留为同级的 `.previous` 目录。存在待确认的游戏操作时拒绝更新；下载和重启期间持有操作锁并暂停新的游戏操作。成功后旧网页会话失效，Windows 再双击一次启动器打开工作台；Linux 执行 `./open-workbench.sh` 获取新链接。

v0.2.0 需要先手动升级至带更新入口的版本，之后才能使用此功能。源码运行模式不能在网页替换源码；继续使用 Git 或下载 Release 更新。更新信任项目的 GitHub Release 和 SHA256 清单，不提供独立代码签名。

## 自动处理哪些任务

支持任务 ID 1–13：登录游戏、星辰护卫、卡杰洛斯、影子副本、混沌地牢/库尔赞前线、极焰的沙漏、累计签到 7/14/21/28 天、首次查看规则、首次组建 4 人小队、小队登录游戏。

任务 6–11 的领取参数依据统一接口推断，尚无逐项成功抓包，不能声称已经真实领奖验证。它们与已收录任务共用 partnergettaskreward2611、task_id、type=1。只在官方返回 status=1 时领取；不会代替玩家完成副本、查看活动规则或组队条件。未知、重复、未完成或已领取任务跳过。

签到成功后刷新任务，可在同次执行领取刚解锁的累计奖励。提交前后重新查询；异常或无法确认成功时立即停止。服务器和本地版持久保存 pending-operation.json，后续先查询确认，结果仍不明确则提示更新 token。成功保存同一游戏账号、同一角色的凭据后清除该账号的待确认限制，下次执行重新查询官方状态。链接参数名兼容大小写（如 accType/acctype），凭据值保持原样，同名参数值相同可合并，值冲突则拒绝。手机在当前执行中停止；不同端之间没有共享锁，请选择一个端为同一账号执行自动计划。

### 登录凭据有效性（v0.3.4）

官方任务接口在 QQ access token 过期时仍可能返回成功，却将每日任务标为未完成。本版四端在查询签到、任务和提交奖励前先校验 QQ 官方 OAuth 身份；服务器和本地版仅短暂缓存成功结果。凭据失效会明确提示更新 token，校验网络异常会暂停本次执行，不把任务显示成未完成。导入时也会在线验证账号与应用身份，验证失败不保存、不解除待确认记录。iPhone 每次运行先验证，首次验证成功才保存链接。

## 源码与构建

- server/：多用户后端、React/Vite 网页、部署单元与测试。
- windows/：Windows 本地后台服务、DPAPI 凭据存储、登录自启及原生构建。
- linux/：Linux 独立本地服务、加密凭据、用户自启及原生构建。
- VERSION：发布版版本号；Windows/Linux 的原生构建会将它嵌入程序。
- windows/service/updater.py / linux/service/updater.py：GitHub 版本检查、SHA256 校验、锁与更新状态。
- windows/service/update_worker.py / linux/service/update_worker.py：旧服务退出后替换程序并启动新版。
- shortcuts/：iPhone 动作图、构建签名工具、模拟与结构测试。
- activity-manifest.json / verify_activity.py：核对四端的活动配置。
- tools/package_release.py：生成 ZIP/tar.gz，包内附 SHA256SUMS.txt。
- .github/workflows/release-build.yml：Windows/Linux 原生构建、单元与成品启动检查、服务器打包；全部成功后校验并发布压缩包与手机签名文件。

各目录有中文注释和使用说明，阅读顺序见 docs/代码阅读指南.md。运行 `python3 verify_activity.py` 检查配置一致。前端未改变功能，已附网页成品；重建时在 web/ 下运行 npm ci，再运行 npm run build -- --outDir ../dist（Node 20.19+/22.12+）。

iPhone 动作图唯一来源是 shortcuts/source/workflow.json。`python3 shortcuts/build.py` 在 macOS 上调用 Apple CLI 签名，生成的文件用于 iPhone；构建机器平台不代表快捷指令仅可在 Mac 使用。不要运行 tools/history 内的历史补丁。签名及模拟测试不能替代手机真机测试。

## 数据与验证边界

发行包不包含真实活动链接、账号、token、Cookie、数据库或抓包原文。Windows 凭据由当前用户 DPAPI 加密；Linux 使用独立用户密钥加密，密钥与密文同机保存，只能保护单独泄露密文的情形。详细数据路径和迁移方式见各端 README。

自动测试不调用真实游戏接口；Windows/Linux 在对应 GitHub 原生环境测试冻结成品的启动、网页、鉴权、GitHub 更新检查、单实例及停止，并用本地模拟发行包验证真实替换、数据保留和自动重启。个人设备的浏览器唤起、自启和 iPhone 全流程需设备验收。推断任务实际可领与否仍以活动官方响应为准。1224 号二进制角色详情请求未接入。

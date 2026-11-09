# 命运方舟助手 Linux 独立版

这是单用户本地服务，不需要注册助手账号。发行包内含 Linux x86_64 可执行程序、浏览器工作台和后台启动脚本，使用者不需要安装 Python。支持 Ubuntu 22.04/24.04 x86_64 及兼容的 glibc 2.35+ 系统；不支持 ARM、Alpine/musl。仅监听 `127.0.0.1`，不直接开放局域网端口。

## 解压和运行

```sh
tar -xzf LostArkHelper-Linux-x64-v0.3.1.tar.gz
cd LostArkHelper-Linux-x64-v0.3.1
./start.sh
```

启动后终端会打印含一次启动会话密钥的工作台链接，用当前电脑的浏览器打开。后台进程脱离终端，关闭终端或网页不会停止定时任务。再次获取链接运行 `./open-workbench.sh`，安全停止运行 `./stop.sh`。也可以直接运行 `./LostArkHelper` 自动打开浏览器，或 `./LostArkHelper --no-browser --port 18745` 在前台启动。

在工作台导入官方活动分享链接，链接里的 token、账号和角色字段会自动解析。导入后开启需要的定时计划。所有计划按北京时间 UTC+8；休眠或停机期间不执行，恢复后合并最近一次错过的计划。首次启用新账号时默认暂停定时任务。

活动版本为 `2611`，领取白名单包含任务 ID 1–13。ID 6–11 的领奖参数由同类接口推断，尚未逐项抓包验证；使用统一 `type=1`，实际领取只针对列表明确显示 `status=1` 的项目。签到后刷新列表，因此本次签到刚满足的累计奖励也能被发现。每次提交后再次查询确认；结果未知会写入 `pending-operation.json` 并停止后续提交，避免重复领取。页面提示前往“账号管理 → 更新我的 token”；成功保存同一游戏账号、同一角色的新链接后，自动清除本机待确认记录，下次执行先重新查询。无效链接、身份不符或保存失败不会清除记录，导入过程不提交签到或领奖。

## 登录后自动启动

需要支持 `systemd --user` 的系统，在解压目录运行：

```sh
./install-user-service.sh
./open-workbench.sh
systemctl --user status lostark-helper.service
journalctl --user -u lostark-helper.service
```

用户服务在当前用户登录后启动。需要退出登录后继续运行或无交互登录时启动，由管理员按需执行 `sudo loginctl enable-linger 用户名`。定时计划仍需在工作台单独启用。

关闭用户服务自启并停止：`./install-user-service.sh --disable`。移动程序目录后重新运行安装脚本。服务管理器配置了异常退出重启；安装用户服务前先用 `./stop.sh` 停止手动后台实例，避免两种启动方式互相影响。用户服务模式请用 `systemctl --user stop lostark-helper.service` 停止；网页停止在启用 linger 的服务模式下不会清除自启配置。

## 无桌面服务器

在 Linux 上启动并运行 `./open-workbench.sh`，然后在自己的电脑建立 SSH 转发（示例默认端口 18745）：

```sh
ssh -N -L 18745:127.0.0.1:18745 用户名@Linux地址
```

保持 SSH 连接，在自己电脑浏览器打开刚输出的 `http://127.0.0.1:18745/open?key=...`。若启动日志实际端口不同，转发的两个端口均改为该端口。浏览器使用的地址必须保持 `127.0.0.1` 和该实际端口，以通过本机来源校验。不要把带 key 的链接公开分享。

## 数据和升级

数据在 `${XDG_DATA_HOME:-~/.local/share}/LostArkHelper`，目录权限 `700`；凭据使用 Fernet 加密存入 `credentials.enc`，密钥文件 `credentials.key` 与密文均为 `600`。不依赖桌面密钥环，因此无桌面 Linux 也能运行。密钥和密文保存在同一用户目录：这不能抵御同一用户、root 或整个数据目录被复制后的访问。备份时按敏感信息处理整个目录，不要上传 GitHub。

`automation.sqlite3` 保存计划和脱敏历史；`instance.json` 保存当前随机本机会话；`service.log` 保存脚本启动日志。升级时先安全停止，替换解压目录内的程序和脚本，保留用户数据目录，再启动。删除程序目录不会删除数据。丢失密钥后原密文无法恢复，需要重新导入。

### v0.3.0 起的网页更新

进入“定时设置 → 版本更新”，点击检查更新；发现新版后确认安装。程序下载 `youboRepo/LostArkHelper` 的正式 Release，核对发布页及包内 SHA256，旧服务完全退出后替换目录并启动新版。数据和历史在独立用户目录中保留，旧目录保留为同级的 `.previous` 目录。

更新后旧会话失效，执行 `./open-workbench.sh` 获取新的工作台链接；systemd 用户服务仍指向原目录，不需要重新安装。网页手动停止后的 systemd 行为与原来一致。v0.2.0 需先手动升级到带网页更新入口的版本；源码运行版继续使用 Git 或手动下载。存在待确认游戏操作时会拒绝更新。

## 从源码构建

在 Ubuntu 22.04 x86_64 使用 Python 3.11 和 Node.js 22：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r linux/requirements-build.txt
cd linux/web
npm ci
npm run build -- --outDir ../dist
cd ../..
bash linux/build-linux.sh
```

原生构建输出 `linux/release/LostArkHelper-Linux-x64.tar.gz`。正式 Release 再由根目录 `tools/package_release.py` 生成带版本号的压缩包及校验清单。构建脚本会先运行单元测试，冻结程序后再执行隔离目录的启动、鉴权、网页资源、重复启动和安全停止测试，不访问游戏接口。构建环境需要 Python 和 Node，发行包运行时不需要。

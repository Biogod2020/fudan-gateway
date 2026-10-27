# FudanGateway aTrust 模式

这里保存已有 aTrust 实现的可构建源码：UIS 原生登录窗口、后台监督与恢复、菜单栏界面、
控制命令、配置示例和隔离测试。WebVPN 模式仍在仓库根目录，MacBook 继续使用 WebVPN。

## 构建和安装

要求 macOS 13+、Xcode Command Line Tools、Git、Go 1.25.6+ 和 Python 3.11+。
在仓库根目录运行：

```sh
atrust/scripts/build-macos.sh
uv run --no-project python atrust/scripts/install.py --dry-run
uv run --no-project python atrust/scripts/install.py
```

构建脚本获取并验证 `upstream.json` 固定的 zju-connect 提交，不使用随时间变化的最新版本。
下载的客户端源码放在 `atrust/.cache/`，构建结果放在 `dist/atrust/`，都不进入 Git。
也可以用 `ZJU_CONNECT_SOURCE=/absolute/clean/checkout` 指向相同提交的干净本地副本。
`GO_BIN` 可指定 Go 可执行文件。

安装命令准备运行目录和两个用户 LaunchAgent，**不会立即启动服务**。它保留已有配置，
备份将被替换的自身文件，拒绝覆盖正在运行的同名服务。安装后，下次用户登录时启动；
首次没有会话时，后台提示登录并停止。安装不会修改既有 WebVPN 或旧 aTrust 部署。

默认运行目录：`~/Library/Application Support/FudanGateway/atrust`。菜单应用：
`~/Applications/FudanGateway aTrust.app`。如需其他目录，使用安装器的 `--runtime` 和
`--app`；直接运行命令或应用时同时设置 `FUDANGATEWAY_ATRUST_HOME`。

## UIS 登录与日常使用

```sh
"$HOME/Library/Application Support/FudanGateway/atrust/bin/fudan-gateway.command" login
"$HOME/Library/Application Support/FudanGateway/atrust/bin/fudan-gateway.command" start
"$HOME/Library/Application Support/FudanGateway/atrust/bin/fudan-gateway.command" status
"$HOME/Library/Application Support/FudanGateway/atrust/bin/fudan-gateway.command" check
"$HOME/Library/Application Support/FudanGateway/atrust/bin/fudan-gateway.command" stop
```

UIS 密码在复旦官方网页输入。一次性 OAuth 回调在 WebKit 消耗前截获，只通过管道交给
客户端，不写日志。aTrust 要求的二级认证仍须完成；WebVPN 会话不能替代它。登录成功后，
本机私有 `client-data.json` 保存会话，目录为 `0700`、文件为 `0600`。

登录完成后可在上述应用中启动后台。全新安装也可以加 `--activate` 立即加载两个服务；
若端口 1080 已有服务，安装器拒绝激活，不会终止原服务。旧版 aTrust 与本模式使用同一个
SOCKS 端口，应先明确停止旧版，再启用这个实例。

代理仅监听 `127.0.0.1:1080`。示例配置关闭 TUN、系统路由、DNS 劫持和全局代理；不编辑
v2rayN 数据库或生成的 JSON，也不切换 Tailscale。按需使用：

```sh
curl --socks5-hostname 127.0.0.1:1080 --noproxy '' \
  http://pai-console.cfff.fudan.edu.cn:32080/
ssh -o 'ProxyCommand=/usr/bin/nc -X 5 -x 127.0.0.1:1080 %h %p' your-campus-host
```

SSH 用户认证与服务器权限仍由 OpenSSH 处理。此安装器不覆盖 SSH 配置；请使用独立别名，
避免覆盖既有的 WebVPN 别名配置。v2rayN 如需校园分流，应在其 GUI 中设置；保留
回环、Tailscale、VPN/UIS 直连，先匹配 CFFF 等校内资源，再匹配公开复旦网页直连。

## 自动恢复和状态含义

- 每五分钟通过 SOCKS 对 PAI 做一次真实 HTTP 检查；可在私有 `health.json` 中填写自己
  有权访问的 `ssh_target`（`host:port`），作为无需登录的 SSH banner 备用检查。
- 两个目标都失败后，隔三十秒复核；连续两轮失败才重建客户端。收到 SOCKS CONNECT
  成功或看到端口监听，都不算校内连通性验证。
- 临时网络故障复用保存的会话，按 10/20/40/80/160 秒退避，之后最多五分钟一次。
  明确需要 UIS 交互时停止重试并通知一次，不循环提交密码。
- 唤醒和网络变化只触发检查；手动停止后不因唤醒自行启动。`stop` 卸载自身后台服务，
  下次显式 `start` 才重新加载。“仅退出界面”保留后台，“退出并停止代理”同时停止后台。
- 重建隧道可能中断已有 SSH；请重新连接。健康检查说明传输可达，不代表业务登录或期刊权限。

`health.json` 示例不包含任何人的校园服务器地址。配置不合法时，后台退出并给出脱敏错误。
状态文件带最近检查时间；运行中的进程不等于持续可用的网络。

## 验证与来源

```sh
cd atrust/supervisor
go test -race ./...
go vet ./...
```

测试在隔离目录中运行真实子进程和假 SOCKS 服务，覆盖断线重试、明确会话失效、启动卡住、
运行锁、取消、停止、退避恢复、HTTP/SSH 备用检查、配置和日志脱敏。构建也校验两个 Swift
程序和应用签名。测试只使用隔离运行目录和临时监听端口，不操作正在运行的代理。

协议客户端使用未修改的 [zju-connect](https://github.com/Mythologyli/zju-connect)，固定提交
`8de443f582d962554f8d86d53cdddf6a11ec82a6`；其源码和许可证保持在上游仓库，采用
[AGPL-3.0](https://github.com/Mythologyli/zju-connect/blob/8de443f582d962554f8d86d53cdddf6a11ec82a6/LICENSE)。
本仓库不上传登录数据、Cookie、密码、私钥、私人路由、运行日志或第三方预编译客户端。

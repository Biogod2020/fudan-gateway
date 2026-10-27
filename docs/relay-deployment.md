# 校内中继部署

WebVPN 的网页 SSH 使用 Guacamole，不直接向本机提供原始 TCP。本项目在有权使用的
校内主机运行一个小型中继，通过 WebVPN 的 WebSocket 资源路径传输原始字节。
OpenSSH 仍在客户端和目标 SSH 服务之间完成密钥交换、主机密钥校验和用户认证。

## 单主机拓扑

如果目标主机的一个用户端口已能从 WebVPN 访问，可以直接在该主机运行中继：

```mermaid
flowchart LR
  A[MacBook OpenSSH] --> B[FudanGateway ProxyCommand]
  B -->|WSS + WebVPN 会话| C[复旦 WebVPN]
  C -->|WS + 一次性请求签名| D[校内中继]
  D --> E[允许列表中的 SSH 服务]
```

在主机安装项目，使用 Python 3.11 或更新版本：

```sh
uv sync --frozen --no-dev
uv run fudangateway keygen /absolute/private/path/relay.key
uv run fudangateway relay --config /absolute/private/path/relay.json
```

`examples/relay.json` 中的监听地址必须改为该机器实际拥有的私有地址。只把自己有权使用
的目标加入 `targets`，不要建立任意地址转发。中继密钥通过可信 SSH 通道交给客户端，
两端文件权限为 `0600`，目录权限为 `0700`。

## 目标主机不接受新增入站端口

可以让一台已能从 WebVPN 访问的校内主机承载中继，目标主机只通过现有 SSH 端口向它
建立反向连接。反向端口绑定到中继主机的 `127.0.0.1`，不向校园其他机器开放。

```mermaid
flowchart LR
  A[MacBook OpenSSH] --> B[WebVPN]
  B --> C[可访问的校内中继]
  C --> D[中继主机回环端口]
  D -->|目标主机主动建立的 SSH 反向连接| E[目标主机回环 SSH 服务]
```

反向连接的专用 SSH 密钥留在目标主机。中继主机只接收公钥，并使用 `authorized_keys`
的 `from`、`restrict`、`permitlisten`、`permitopen` 和固定 `command` 限制它：

```text
from="TARGET_PRIVATE_IP",restrict,port-forwarding,permitlisten="127.0.0.1:2222",permitopen="127.0.0.1:1",command="exec /absolute/venv/bin/fudangateway relay --config /absolute/private/relay.json" ssh-ed25519 PUBLIC_KEY
```

需要 OpenSSH 7.8 或更新版本支持 `permitlisten`。专用连接可以同时带上：

```sh
ssh -T -o BatchMode=yes -o IdentitiesOnly=yes -i /private/relay-link-key \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/private/known_hosts \
  -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:2222:127.0.0.1:2222 relay-user@relay.campus.example relay
```

中继配置的 `watch_parent: true` 使其在启动它的 SSH 服务端进程退出后清理并退出，
避免遗留监听端口。目标主机可用 `systemd --user` 的 `Restart=always`、`RestartSec=10`
和已有用户 linger 管理这条连接。不需要中继主机具有用户 systemd 或 cron。

## 可选的用户级 SSH 服务

如果系统 SSH 的新公钥登录受服务器策略限制，在得到相应权限的个人账户下，可以运行
仅监听回环地址的 OpenSSH 实例。这个实例只允许自己的用户和客户端公钥，不修改
系统 sshd、PAM 或 SELinux。示例：

```text
ListenAddress 127.0.0.1
Port 2222
HostKey /private/ssh_host_ed25519_key
AuthorizedKeysFile /private/authorized_keys
AllowUsers YOUR_SERVER_USER
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
StrictModes yes
AllowTcpForwarding yes
AllowAgentForwarding no
X11Forwarding no
Subsystem sftp internal-sftp
```

先用 `sshd -t -f /private/sshd_config` 检查，再由 `systemd --user` 启动。主机密钥应在
可信的现有连接中取得公钥并固定到客户端的独立 `known_hosts` 文件。客户端私钥不上传
到 WebVPN，也不复制到中继。

## 生命周期和边界

- 中继仅接受预先配置的目标；每次建连使用带时间窗和随机数的 HMAC 请求签名，拒绝重放。
- 停止或重启中继会中断现有 SSH；连接恢复后应新建 SSH 会话，不声称无损恢复。
- 不安装 TUN，不修改系统代理、DNS 或全局路由。
- 没有为 SSH 实现 UDP 或任意地址 SOCKS；应用访问范围由允许列表和目标账户权限决定。
- 若服务器策略不允许用户中继或额外端口，应先由管理员提供允许的端点，不修改或绕过防火墙。

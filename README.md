# doro · QQ 官方机器人 / Antigravity 桥接

独立的 Windows 桌面程序，通过腾讯官方 WebSocket / OpenAPI 接收 QQ **私聊和群内 @消息**，调用 Google 官方 Antigravity CLI (`agy`) 生成文字回答。不依赖 NapCat、普通 QQ 小号或 DeepSeek Harness。

## 首次使用

1. 双击 `start.cmd`。首次运行会安装独立 Python 环境与腾讯 SDK，需要 Python 3.11+ 和网络。
2. 在窗口填写机器人 **AppID、AppSecret**。AppSecret 用 Windows DPAPI 加密保存在 `.local/config.json`，绑定当前 Windows 用户；不要把整个 `.local` 目录分享给他人。
3. 点 **测试 Antigravity**。若提示登录不可用，双击 `login-antigravity.cmd`，在官方 CLI 中完成登录，再测试。模型留空使用 CLI 默认模型；也可填写 `agy models` 中列出的模型 ID。使用你的 Antigravity 登录与额度，本程序不提供模型额度。
4. 点 **启动桥接**。在手机 QQ 打开机器人的资料卡（管理页名称旁的二维码可用于打开），按页面的添加/发消息入口进入私聊。若后台限制服务范围，请将你自己的 QQ 加入「内部体验号码」。
5. 将窗口显示的 **配对码** 发到机器人私聊。看到“配对成功”后，再发“你好”。配对码 10 分钟有效；过期后再发一条消息，窗口会生成新码。

首次配对的 QQ 用户是管理员，可邀请其他 QQ 用户私聊；群聊需另外授权。更换机器人 AppID 或点击「解除配对」会清空管理员配对、其他账号的私聊授权和群授权。

## 授权其他 QQ 私聊

1. 管理员在机器人私聊中发送 `/invite`，获取一条 `/join 邀请码` 指令。
2. 把完整的 `/join 邀请码` 发给对方，让对方在自己的机器人私聊中发送这条指令。邀请码 5 分钟有效、只能使用一次，再次发送 `/invite` 会替换旧邀请码。
3. 对方收到授权成功后即可私聊。授权保存到配置中，重启后继续有效。各账号有独立的聊天记忆，私聊 `/reset` 持久清空当前账号的上下文，归档仍可查询。

管理员私聊发送 `/users` 查看已授权的其他账号，按最新列表的序号发送 `/revoke 序号` 撤销授权。撤销会持久清空该账号的上下文，并丢弃仍在排队或生成中的旧回复。重新授权后，上下文从空开始，原有归档可查询。其他私聊账号可以聊天，由管理员管理私聊授权和群绑定。

如果 QQ 后台的服务范围限制了对方使用，先将其加入「开发体验号码」或调整对应服务范围。

## 加入群聊

1. 手机 QQ 打开 doro 资料卡，选择「添加到群聊／添加到本群」，选目标群。入口缺失时，到机器人后台「服务范围」检查群聊范围。个人自用先选择自己是群主或管理员的群；不同认证身份可用范围以 QQ 后台为准。
2. 确保桥接已启动，并已完成上面的私聊配对。用配对账号**私聊** doro 发送 `/group`。
3. doro 会私聊返回 `/bind 绑定码`。在目标群里先从 QQ 成员列表 **@doro**，然后输入这条绑定指令。绑定码 5 分钟有效，只能成功使用一次。
4. 收到“群绑定成功”后，群内任何成员都可以 **@doro 你好** 来聊天。普通群消息不触发回复。

私聊 `/groups` 查看已授权群及当前序号；私聊 `/ungroup 序号` 解除某个群，序号以最近一次 `/groups` 结果为准。取消授权会持久清空该群在桥接中的上下文，并停止排队或生成中的旧回答发送。再次启用需重新绑定，上下文从空开始，原有归档可在重新授权后查询。

每个群使用自己的上下文，群内 `@doro /reset` 持久清空本群记忆，不影响其他群或私聊。归档仍可查询。不需要为此打开全量群消息、主动发言权限。

## 日常使用

- 保持窗口和电脑运行、网络连通；点「停止」或关闭窗口会停止桥接。
- 支持已授权账号的文字私聊及授权群的 @聊天，各账号、各群最近约 6 轮上下文会从本地归档独立恢复。发送 `/reset` 持久清空当前会话上下文。
- 暂不处理图片、语音、文件或操作电脑。文字回复超过 1800 字会明确截断。
- 窗口显示运行状态，已完成的文字对话保存为本地聊天归档。QQ 聊天文本会发送给 Antigravity 使用的模型服务。
- 不同群、私聊最多同时处理 2 条请求，同一会话按顺序回复，全局最多等待 8 条。单次生成最多 120 秒。日常聊天默认简短回答，明确要求详细解释时按需展开。SDK 自动保持心跳、刷新 Token 和重连。

### 回复耗时

调用模型的聊天请求结束后，后台输出一条 `timing` JSON 日志，记录接收后等待入队的 `admission_seconds`、排队的 `queue_seconds`、等待授权锁的 `authorization_seconds`、整次 CLI 调用的 `model_seconds`、QQ 发送的 `delivery_seconds` 和总耗时 `total_seconds`，单位均为秒。`model_seconds` 包含 CLI 启动、模型服务等待、生成和退出时间。`outcome` 标记发送、失败或丢弃等结果。

Linux 服务可使用以下命令查看耗时日志。

```bash
journalctl -u qq-antigravity-bridge -n 100 --no-pager | grep '"event": "timing"'
```

## 聊天记录与记忆

启动桥接时自动创建 `.local/history.sqlite3`，使用 Python 标准库 SQLite 保存聊天归档。记录按机器人 AppID、配对管理员、聊天类型和会话 ID 隔离，私聊与各群分别查询和恢复。

首次启用后，归档从新完成的文字对话开始积累。归档范围为已授权私聊账号和授权群内 @机器人的已完成文字对话，包括用户文字和实际发送回答的完整内容。QQ 回答发送成功后写入归档。归档内容仅包括聊天问答，配对、绑定、查询和清理指令由命令处理流程直接执行。

重启后自动恢复最近约 6 轮对话用于模型上下文，历史上下文的 JSON 总长度控制在 24000 字符内。长期归档保留到用户通过清除指令删除。`/reset` 持久清空当前会话上下文，归档仍可查询。

### 查询当前会话

已授权账号在私聊中发送下列指令。群成员在目标群从成员列表 @doro 后，可以查询本群归档或重置本群上下文。

| 指令 | 用途 |
| --- | --- |
| `/history [页码]` | 分页查看当前会话摘要，每页最多 5 轮 |
| `/history search 关键词` | 查看最近匹配关键词的 5 轮对话摘要 |
| `/history show 记录ID [页码]` | 分页查看指定记录的完整文字，记录 ID 见查询结果 |
| `/reset` | 持久清空当前会话上下文，保留归档 |
| `/history clear` | 私聊删除当前账号会话的全部归档和上下文 |

### 管理员管理群归档

管理员先私聊发送 `/groups` 获取当前群序号，再在私聊中使用以下指令。群归档清理由管理员私聊操作，查询和全文查看均限定在选定的授权群。

| 指令 | 用途 |
| --- | --- |
| `/history group 群序号 [页码]` | 分页查看该群的对话摘要 |
| `/history group 群序号 search 关键词` | 查看该群最近匹配关键词的 5 轮对话摘要 |
| `/history group 群序号 show 记录ID [页码]` | 分页查看该群指定记录的完整文字 |
| `/history group 群序号 clear` | 删除该群的全部归档和上下文 |

Antigravity CLI 自身会按其设置保存独立会话。桥接清理指令的作用范围是本地聊天归档和桥接上下文，CLI 会话与 QQ 客户端记录由各自的管理功能处理。

## 接入设置

在 QQ 机器人后台「开发设置 → 事件订阅与回调地址」使用 **WebSocket**，开放所需的私聊、群 @消息事件（如果页面要求选择）。如果配置了 IP 白名单，需要允许本机的公网出口 IPv4。未配置白名单时按 QQ 当前规则处理，无需填写 localhost。

本版使用 Antigravity 的纯文字聊天方式：每轮是新 CLI 会话，上下文由桥接程序提供。CLI 会话覆盖 `toolPermission=strict` 并拒绝命令、文件、URL 与 MCP 工具调用，没有使用跳过审批参数。这不构成操作系统沙箱：Antigravity 仍使用本机现有账号配置，全局 hooks / MCP 服务可能由 CLI 自身加载。

## 本地验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe bridge.py version
```

测试使用模拟 QQ 和 CLI，不向 QQ 发消息。真实收发需配置凭据后在窗口启动，并通过 QQ 完成配对。

## 官方资料

- [QQ 机器人官方文档](https://bot.q.qq.com/wiki/)
- [腾讯官方 Python SDK](https://github.com/tencent-connect/qqbot-agent-sdk)
- [Antigravity CLI headless](https://antigravity.google/docs/cli/headless/)
- [Antigravity 权限](https://antigravity.google/docs/permissions/)

目录内包含配置窗口、官方 QQ 连接、CLI 适配、配对及测试；与其他 QQ 项目的配置、数据库、依赖独立。

## CentOS 7 云服务器部署

`centos7` 分支增加 Linux 无界面运行支持。部署目录约定为 `/opt/qq-antigravity-bridge`，服务账号为 `qqbridge`，账号主目录为 `/home/qqbridge`。下面的 systemd 文件使用这些路径。

安装脚本在项目 `.runtime` 中安装 uv 和独立 Python 3.11，并创建 `.venv`。uv 固定为 0.13.7，从 Astral 官方镜像下载，显示下载进度并校验 SHA256，单次下载限时 180 秒，失败后最多重试两次。uv 的 x86_64 Linux 构建和对应的 Python 构建支持 glibc 2.17，项目依赖安装限定为兼容 `manylinux_2_17` 的预编译 wheel。Antigravity 使用官方 `linux_amd64_musl` 清单中的静态构建，逐次核对 SHA512，并在安装完成前执行 `agy --version`。该分支需要在目标 CentOS 7 机器上完成 CLI 登录、模型生成和 QQ 收发验收。[uv 平台支持](https://docs.astral.sh/uv/reference/policies/platforms/)、[Python 构建运行要求](https://gregoryszorc.com/docs/python-build-standalone/main/running.html)

### 获取源码并安装运行环境

服务器需要 `git`、`curl`、`tar`、`gzip`、`flock`、`sha256sum` 和有效的 CA 根证书，以及访问 GitHub、Astral、Google 官方下载和模型服务、QQ 服务的网络连接。

使用 root 创建服务账号和部署目录。

```bash
useradd --system --user-group --create-home --home-dir /home/qqbridge --shell /bin/bash qqbridge
install -d -o qqbridge -g qqbridge -m 755 /opt/qq-antigravity-bridge
su - qqbridge
```

在 `qqbridge` 账号下通过 HTTPS 拉取公开仓库并安装运行环境。后续更新同样在 `qqbridge` 账号下执行。

```bash
git clone --branch centos7 https://github.com/justdoit712/QQ-Antigravity-Bridge.git /opt/qq-antigravity-bridge
cd /opt/qq-antigravity-bridge
bash deploy/centos7-install.sh
```

脚本生成 `.local/server.env`，文件权限为 `0600`。使用 `vi .local/server.env` 填写服务器配置。

```bash
QQBOT_APP_ID='你的机器人AppID'
QQBOT_APP_SECRET='你的机器人AppSecret'
# QQBOT_MODEL='可选的模型ID'
```

该文件采用 shell 和 systemd 均支持的变量赋值格式，变量值用单引号包围。Linux 的配对账号及群授权保存在 `.local/config.json`，聊天归档保存在 `.local/history.sqlite3`，机器人密钥由环境变量提供。

### 登录并测试

继续在 `qqbridge` 账号下运行，按官方 CLI 提示完成登录，然后退出 CLI 回到终端。

```bash
export PATH="$PWD/.runtime/agy/bin:$PATH"
export AGY_CLI_DISABLE_AUTO_UPDATE=true
agy
```

CLI 使用缓存的登录凭据进行无界面调用。Linux 凭据访问若报告 keyring 或 D-Bus 错误，按官方故障排查配置账号的密钥服务，并让 systemd 服务使用该账号的凭据访问环境。[CLI 无界面运行](https://antigravity.google/docs/cli/headless/)、[CLI 故障排查](https://antigravity.google/docs/cli/troubleshooting)

加载机器人配置，先测试模型生成，再启动 QQ 收发测试。配对及群授权流程见上文。

```bash
set -a
. .local/server.env
set +a
.venv/bin/python bridge.py check-agy
.venv/bin/python bridge.py run
```

测试完成后按 Ctrl+C 停止桥接，再执行 `exit` 返回 root 账号。

### 配置开机启动

使用 root 安装服务并启动。

```bash
cd /opt/qq-antigravity-bridge
install -m 644 deploy/qq-antigravity-bridge.service /etc/systemd/system/qq-antigravity-bridge.service
systemctl daemon-reload
systemctl enable qq-antigravity-bridge
systemctl start qq-antigravity-bridge
journalctl -u qq-antigravity-bridge -n 50 --no-pager
```

服务通过 `--daemon` 处理 SIGTERM 和 SIGINT，systemd 管理异常重启及进程组退出。查看实时日志使用 `journalctl -u qq-antigravity-bridge -f`，停止服务使用 `systemctl stop qq-antigravity-bridge`。

服务文件设置 `AGY_CLI_DISABLE_AUTO_UPDATE=true`，使 CLI 版本更新继续走安装脚本中的 musl 构建和 SHA512 校验流程。后续更新时，以 root 停止服务，以 `qqbridge` 账号拉取 `centos7` 分支并重新运行安装脚本，完成模型检查后再由 root 启动服务。

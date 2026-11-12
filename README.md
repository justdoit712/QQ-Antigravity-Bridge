# doro · QQ 官方机器人 / Antigravity 桥接

独立的 Windows 桌面程序，通过腾讯官方 WebSocket / OpenAPI 接收 QQ **私聊和群内 @消息**，调用 Google 官方 Antigravity CLI (`agy`) 生成文字回答。不依赖 NapCat、普通 QQ 小号或 DeepSeek Harness。

## 首次使用

1. 双击 `start.cmd`。首次运行会安装独立 Python 环境与腾讯 SDK，需要 Python 3.10+ 和网络。
2. 在窗口填写机器人 **AppID、AppSecret**。AppSecret 用 Windows DPAPI 加密保存在 `.local/config.json`，绑定当前 Windows 用户；不要把整个 `.local` 目录分享给他人。
3. 点 **测试 Antigravity**。若提示登录不可用，双击 `login-antigravity.cmd`，在官方 CLI 中完成登录，再测试。模型留空使用 CLI 默认模型；也可填写 `agy models` 中列出的模型 ID。使用你的 Antigravity 登录与额度，本程序不提供模型额度。
4. 点 **启动桥接**。在手机 QQ 打开机器人的资料卡（管理页名称旁的二维码可用于打开），按页面的添加/发消息入口进入私聊。若后台限制服务范围，请将你自己的 QQ 加入「内部体验号码」。
5. 将窗口显示的 **配对码** 发到机器人私聊。看到“配对成功”后，再发“你好”。配对码 10 分钟有效；过期后再发一条消息，窗口会生成新码。

私聊只允许一个配对 QQ 用户使用；群聊需另外授权。更换机器人 AppID 或点击「解除配对」会清空私聊配对和群授权。

## 加入群聊

1. 手机 QQ 打开 doro 资料卡，选择「添加到群聊／添加到本群」，选目标群。入口缺失时，到机器人后台「服务范围」检查群聊范围。个人自用先选择自己是群主或管理员的群；不同认证身份可用范围以 QQ 后台为准。
2. 确保桥接已启动，并已完成上面的私聊配对。用配对账号**私聊** doro 发送 `/group`。
3. doro 会私聊返回 `/bind 绑定码`。在目标群里先从 QQ 成员列表 **@doro**，然后输入这条绑定指令。绑定码 5 分钟有效，只能成功使用一次。
4. 收到“群绑定成功”后，群内任何成员都可以 **@doro 你好** 来聊天。普通群消息不触发回复。

私聊 `/groups` 查看已授权群及当前序号；私聊 `/ungroup 序号` 解除某个群，序号以最近一次 `/groups` 结果为准。取消授权会清空该群在桥接中的上下文，并停止排队或生成中的旧回答发送；再次启用需重新绑定。

每个群使用自己的上下文，群内 `@doro /reset` 只清空本群记忆，不影响其他群或私聊。不需要为此打开全量群消息、主动发言权限。

## 日常使用

- 保持窗口和电脑运行、网络连通；点「停止」或关闭窗口会停止桥接。
- 支持文字私聊及授权群的 @聊天，各自最近 6 轮上下文保存在内存中；重启会清空，发送 `/reset` 清空当前会话。
- 暂不处理图片、语音、文件或操作电脑。文字回复超过 1800 字会明确截断。
- 窗口只显示运行状态，不保存聊天原文日志。Antigravity CLI 自身会按其设置保存会话；QQ 聊天文本会发送给 Antigravity 使用的模型服务。
- 请求串行处理，最多等待 8 条；单次生成最多 120 秒。SDK 自动保持心跳、刷新 Token 和重连。

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

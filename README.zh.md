# dsh-feishu-bridge

[English](README.md) | 中文

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）的飞书（Lark）channel 桥：给飞书机器人发消息，触发一次 `dsh` agent turn，回复自动发回该聊天。

**这是一个独立的社区项目，不由 DeepSeek 官方构建、维护或背书。** 它完全通过 `dsh` 的公开 Python SDK（`deepseek-harness-sdk`）驱动——子进程边界，没有 fork/patch harness 本身的代码。

## 这是什么

- 一套生产级飞书机器人桥（fail-closed 白名单、一次性卡片 nonce、per-chat 输出详略、sticky session、`ws`/`webhook` 双 transport），从另一个 agent harness 项目的成熟飞书桥移植而来，改接到 `dsh`。
- 与 `deepseek-harness-sdk` 对话的薄适配层集中在一个文件 `src/dsh_feishu_bridge/dsh_adapter.py`，SDK 版本精确锁定——harness 目前是 v0.1 developer preview，版本间明示会有破坏性变更。

## 5 分钟快速上手

```sh
git clone <this-repo-url> dsh-feishu-bridge
cd dsh-feishu-bridge
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

凭据一律走环境变量——绝不写进任何要提交的文件：

```sh
export DEEPSEEK_API_KEY=sk-your-key-here
# export DEEPSEEK_BASE_URL=http://127.0.0.1:8000/v1   # 仅在走代理时需要

export FEISHU_APP_ID=cli_xxxxxxxx
export FEISHU_APP_SECRET=xxxxxxxx
export FEISHU_TRANSPORT=ws          # 或 "webhook"（需要一个公网可达的 URL）
# export FEISHU_VERIFICATION_TOKEN=xxxx   # FEISHU_TRANSPORT=webhook 时必填
# export FEISHU_ENCRYPT_KEY=xxxx          # 可选，若开启了事件加密

# fail-closed 白名单——必填。不配置任何 id 时机器人对谁都不回复；
# 每条消息都会被拒绝，这是设计如此（见下方"安全姿态"）。
export FEISHU_ALLOWED_OPEN_IDS=ou_xxxxxxxxxxxxxxxx
# export FEISHU_ALLOWED_CHAT_IDS=oc_xxxxxxxxxxxxxxxx   # 可选的群聊白名单
```

运行：

```sh
python -m dsh_feishu_bridge
# 或: dsh-feishu-bridge
```

在飞书里给机器人发消息。未在白名单里的 `open_id` 发来的第一条消息会被静默拒绝并记录日志——这条日志就是你查到自己 `open_id` 的方式（见下方"获取你的 open_id"）。

### 获取你的 open_id

先给机器人发一条消息（它不会回复——这是预期行为，fail-closed）。在服务端日志里找这样一行：

```
Feishu: rejecting message from unauthorized open_id=ou_xxxxxxxxxxxxxxxx (chat=oc_xxxx)
```

把这个 `open_id` 填进 `FEISHU_ALLOWED_OPEN_IDS`，重启即可。

## 命令

| 命令 | 作用 |
|---|---|
| `/new [名称]` | 开始一个新会话 |
| `/sessions` | 列出会话（点击切换） |
| `/switch <id>` | 切换到某个已有会话 |
| `/current` | 查看当前会话信息 |
| `/quiet` | 只显示回复（默认） |
| `/verbose` | 同时显示状态/结果行 |
| `/help` | 列出命令 |

## 配置参考

全部走环境变量。可选的 YAML 文件（路径通过 `DSH_FEISHU_BRIDGE_CONFIG` 或 `--config` 指定）可以配置非敏感项（白名单、model、provider）——见 `examples/config.example.yaml`。两者都设置时环境变量优先，凭据故意不从 YAML 文件读取。

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 必填。和 SDK 自身读取的变量同名。 |
| `DEEPSEEK_BASE_URL` | — | 可选，用于 OpenAI 兼容代理。 |
| `DSH_PROVIDER` | `deepseek-official` | Provider 路由（见 SDK 文档）。 |
| `DSH_MODEL` | `deepseek-v4-flash` | 模型 id。 |
| `DSH_MAX_TOKENS` | 未设置 | 可选的单请求输出上限。 |
| `DSH_CORDIS` | 未设置 | 自定义 Cordis composition 路径；不填则用内置默认。 |
| `DSH_SESSION_ROOT` | 未设置 | runtime 写 JSONL session 日志的目录。 |
| `DSH_WORKSPACE` | 当前目录 | agent 工具操作的工作区。 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | — | 必须同时配置，或都不配置。 |
| `FEISHU_TRANSPORT` | `ws` | `ws`（无需公网 URL）或 `webhook`。 |
| `FEISHU_VERIFICATION_TOKEN` | — | `FEISHU_TRANSPORT=webhook` 时必填。 |
| `FEISHU_ENCRYPT_KEY` | 未设置 | 可选，若开启了事件加密。 |
| `FEISHU_DOMAIN` | `https://open.feishu.cn` | Lark 国际版或走代理时修改。 |
| `FEISHU_ALLOWED_OPEN_IDS` | *(空)* | 逗号分隔。**必填**——为空则无人被授权。 |
| `FEISHU_ALLOWED_CHAT_IDS` | *(空 = 不限制)* | 逗号分隔的群聊白名单。 |
| `DSH_FEISHU_BRIDGE_HOST` | `0.0.0.0` | HTTP 服务绑定地址（健康检查 + webhook 路由）。 |
| `DSH_FEISHU_BRIDGE_PORT` | `8788` | HTTP 服务端口。 |

## 安全姿态

- **默认 fail-closed。** 不配置 `FEISHU_ALLOWED_OPEN_IDS` 意味着所有发送者都被拒绝——没有隐式的"允许所有人"。这是刻意设计：一个白名单为空的 agent 桥如果默认放行，会让租户里任何人都能触发任意 agent turn。
- **webhook 模式必须有 verification token。** 没有的话 webhook 路由根本不会注册——进程宁可拒绝以半配置状态启动,也不会默默接受未经验证的事件。
- **卡片按钮（会话切换）使用一次性、绑定身份的 nonce。** nonce 铸造时精确绑定某个 action + session；二次点击、重放的 nonce、被篡改的卡片 value 都会被拒绝且不生效。
- 用这个 bridge 的进程时用composition 实际需要的最小权限运行。内置默认的 `dsh` composition（`examples/jsonrpc-agent` 上游）用的是 `danger-full-access` 的 bash + 文件编辑——请在一次性工作区/容器里跑，不要对着你在意的机器跑。

## 限制（v1，刻意为之）

这些是由 `deepseek-harness-sdk` v0.1 当前实际能力决定的范围收窄，在此明确写出而非静默缺失：

- **不支持增量流式输出。** `DeepSeekHarness.run()` 是同步调用，阻塞到该 turn idle 才返回；SDK 的 `on_notification` 钩子能在调用过程中拿到原始协议 notification，但其事件 schema 不属于 v0.1 的既定文档契约。所以 bridge 在 turn 开始时发一条状态行，turn 结束后发完整回复——不是某些桥那种逐 token 流式。
- **不支持工具审批流程。** 内置示例 `dsh` composition 跑 Bash/编辑器工具时没有交互式审批提示，SDK 的高层 `Session` API 也没有暴露"服务端发起审批请求"的钩子（即便未来某个 composition 加了这个能力，也只有底层 `HarnessClient.next_request()/respond()` 才能接住）。批准/拒绝卡片的机制已移植并有单测覆盖(接口对等)，但目前没有任何 session backend 会触发它。
- **会话仅在单个 bridge 进程内 sticky。** 重启会起一个全新的 `DeepSeekHarness` 子进程；通过共享 `session_root` 跨重启恢复并不是 SDK v0.1 文档承诺的行为，所以本桥不会在其之上搭建未经证实的持久化。聊天的 sticky session 指针和它的 `/quiet`/`/verbose` 偏好都会在重启后重置。
- **仅支持文本消息**——不支持语音/图片/文件附件，也不支持话题/子话题回复（一个聊天只有一个 sticky session，跨话题共享会悄悄串台）。
- **每个 bridge 进程只有一份模型配置**——provider/model/cordis composition 是进程级的，不是按聊天区分的。没有 `/agent` 式的重新绑定命令；如果需要第二份配置，跑第二个 bridge 进程（不同端口、不同飞书 app 或白名单）。

## 开发

```sh
pip install -e ".[dev]"
pytest                       # 快——不联网、不起子进程、不烧 API 配额
pytest -m real_sdk           # 真机冒烟测试：需要 DEEPSEEK_API_KEY + runtime；否则自动跳过
```

测试套件把两端都 fake 掉了：一个可编排的 `DshBackend` 顶替真实 SDK（不起子进程、不烧配额），一个本地 `FakeFeishuServer` 顶替 `open.feishu.cn`，断言 bridge 实际发出的出站请求。见 `tests/`。

如果你的网络走代理（比如 Clash）且没有为 `127.0.0.1`/`localhost` 配置豁免，运行涉及 loopback 服务器的测试前先 `export no_proxy=127.0.0.1,localhost`——否则代理会吞掉 bridge 自己发往 fake server 的出站请求。bridge 本身在运行时已经对 loopback 域名强制 `trust_env=False`，所以这只影响测试进程。

## License

MIT——见 [LICENSE](LICENSE)。

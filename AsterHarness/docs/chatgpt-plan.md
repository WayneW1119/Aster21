# 使用 ChatGPT 套餐

Aster 支持 OpenAI 官方 Sign in with ChatGPT 的本地个人/开源应用流程。用户在浏览器授权 Aster 使用套餐额度，Aster 直接通过 OAuth 调用公开 Responses API；不调用 ChatGPT backend-api，不复用 Codex 登录凭据，不回退到 API Key 计费。

## 登录与最小验证

安装可选依赖：

```bash
python -m pip install -e '.[chatgpt]'
```

在运行 Aster 的电脑上执行：

```bash
aster auth login --no-browser
```

用这台电脑的浏览器打开终端给出的登录链接，选择自己的 ChatGPT 账户及 workspace，并授权 ChatGPT plan usage。WSL 用户可使用同一 Windows 电脑的浏览器；默认回调是 `http://127.0.0.1:54321/auth/callback`，不要在另一台设备上打开链接。回调端口可以用 `--port` 更换。

如果 Windows 浏览器无法访问 WSL 回调，可在另一个 Windows PowerShell 窗口运行可选辅助脚本。它无需管理员权限，仅监听 Windows `127.0.0.1`，默认 10 分钟后退出。以下路径都是占位示例，需要替换成自己的 WSL 发行版、项目位置和 Python 解释器：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "\\wsl.localhost\YOUR-DISTRO\path\to\Aster\scripts\chatgpt_loopback.ps1" -Distro "YOUR-DISTRO" -WslPython "/path/to/venv/bin/python" -HelperPath "/path/to/Aster/scripts/chatgpt_loopback_stdio.py"
```

`HelperPath` 是 WSL 内辅助 Python 文件的绝对路径；`WslPython` 默认 `python3`；不指定 `Distro` 时使用 Windows 当前默认的 WSL 发行版。它不是运行 Aster 的前提，只用于特定 WSL 本机回调连通问题。

保持该窗口运行直到授权完成。转接只通过 `wsl.exe` 的标准输入输出连接 WSL 内部回调，不更改防火墙或路由器，也不记录 HTTP 请求内容。

```bash
aster auth status
aster auth models
aster auth probe --model '<模型列表中的 slug>'
```

`probe` 会实际使用少量套餐额度。未指定模型时使用账户模型列表的第一个可显示模型。只有完整收到 `response.completed` 才报告成功；列出模型不等于已验证推理权限。界面中的套餐使用及额度由用户在 <https://chatgpt.com/settings/usage> 管理。

`--profile NAME` 可区分不同账户/注册，`--home PATH` 可指定认证数据所在 Aster home。再次登录同一 profile 会沿用已注册的 client ID；换账户应使用另一个 profile。`aster auth logout` 撤销可续期会话并清除本地令牌；撤销未确认时会明确报告，可以在 ChatGPT 设置中断开连接。

## 配置 Agent

在模板或 Thread 的 `thread.toml` 设置：

```toml
[model]
auth = "chatgpt"
name = "<aster auth models 返回的 slug>"
url = "https://api.openai.com/v1/responses"
auth_profile = "default"
# 可选：指定登录时使用的 --home。省略则使用 ASTER_HOME / 当前默认 home。
# auth_home = "/home/you/.aster"
```

这个模式无需填写 API Key，已有 api_key_env/env_file 不参与该认证。只能向官方 Responses endpoint 发送此 OAuth 凭据。读取配置时会固定认证目录，并将其 credentials 目录加入 Agent 沙箱的私有路径保护。编辑已有 Thread 配置后按原流程 reload/resume；不会自动切换正在运行的其他实例。

SDK 示例：

```python
from aster import Aster, ModelConfig

aster = Aster()
agent = aster.create(
    "./workspace",
    model=ModelConfig(
        name="<账户模型 slug>",
        auth="chatgpt",
        auth_profile="default",
    ),
    instructions="你是我的资料维护助手，用中文交流。",
)
```

示例 [chatgpt_plan.py](../examples/sdk/chatgpt_plan.py) 会在独立测试目录中创建一个小文件并读取验证，输出消息与完整 trace。请使用独立测试目录；实际可用性以登录后的请求结果为准。

## 临时认证故障

认证端点的网络中断、HTTP 429/5xx，以及获取 ID token 签名公钥的临时连接错误，会由 Session 保留同一请求并退避重试。签名、issuer、audience、expiry 和身份校验仍全部执行，永久认证错误会明确失败。

刷新响应包含新的 ID token 时，先将响应作为待校验状态保存在权限为 `0600` 的凭据文件中；校验成功前不会启用这些凭据。公钥服务暂时不可达时，下次重试继续校验同一响应，避免重复使用已经轮换的旧 refresh token。重启进程也保留待校验状态；`status` 不输出令牌。完整失败 checkpoint 的显式恢复方式见 [SDK 文档](sdk.md#生命周期与恢复)。

时间声明允许 5 秒时钟容差，用于处理签发端与本机的秒级偏差；不关闭签名、issuer、audience、nonce 或身份校验。超出容差的未来时间及过期身份仍不能启用。如果暂存身份已过期，账户锁内仅用暂存响应中的最新 refresh token 续换一次，保存新轮换结果后完整验证新的 ID token。旧的过期身份不被启用，也不回退使用已轮换的旧 refresh token；缺少最新刷新凭据、缺少新的身份令牌或新结果仍不合法时明确失败，不循环续换。多个进程共享这一锁和受保护状态。

## 认证与工具适配

- 每次登录使用新的 state、nonce、PKCE verifier；回调只监听 127.0.0.1，校验 state 和返回的 client ID；ID token 验证签名、issuer、audience、expiry、nonce及账户身份。
- 首次动态注册使用 Aster 名称及稳定 host ID，保存服务端发放的 client ID 后才交换 code。令牌和注册保存于 `<home>/credentials/chatgpt/`，权限 0600；状态和 trace 不打印令牌。
- 近到期时刷新令牌；跨进程用文件锁串行化刷新，并原子保存轮换后的 refresh token。授权或刷新失败会报错，不切换其他认证或计费来源。
- 套餐接口要求 `store=false`、`stream=true`，并发送完整 input。Aster 保留自己的事件、记忆、历史和 compact 逻辑。
- 公开套餐接口不接受原生 Responses `tool_search`。仅在传输层把它转换为普通 `functions.tool_search`，搜索结果转换为工具回执和 `additional_tools`，收到调用后转回 Aster 内部事件形式。顶层普通函数也组合到 functions namespace。压缩后的有效历史中没有搜索结果，就不再发送对应 additional_tools。

目前实现适用于 Linux/WSL；多进程锁使用 flock。官方套餐能力仍受账户、workspace、模型和用量限制。商业或对外托管应用的接入条件见官方文档。

官方来源：

- <https://developers.openai.com/siwc/token-sharing-open-source/sign-in>
- <https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference>
- <https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions>
- <https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations>

已有兼容服务的 Thread 切到 ChatGPT 时，传输层会去掉历史工具调用的响应元数据（如 `status` 和调用项 `id`）；对使用 UUID 标识的外部模型 reasoning，仅将已有的明文内容作为 assistant 历史发送，不转发其不可跨服务解密的状态。原始归档保持不变，工具的 `call_id` 和回执配对保持不变。验证历史请求时必须包含已经排队的工具回执，不能把尚未进入历史区的真实回执误判为缺失。

历史调用可能把限定名 `namespace::tool` 放在单个 `name` 字段中，套餐接口不接受其中的冒号。0.1.13 起，传输投影将这种明确的限定名拆为 `namespace` 和 `name`；不改变调用 ID、参数和已有的成功或失败回执，也不重放调用。已有 namespace 与限定名冲突时不猜测覆盖。

官方直连流有时在 `response.completed` 中返回空 `output`，但已通过 `response.output_item.done` 交付完整输出项。ChatGPT 适配器显式启用按索引收集完整输出项的兼容方式，仍要求成功结束、索引连续、没有未完成或重复输出项；不会根据文本或参数增量执行工具，非空汇总与完整项冲突时仍报错。普通 Responses 适配器默认保持严格汇总校验。

## 提示词缓存会话

ChatGPT 适配器为每个持久 Thread 使用稳定的缓存会话标识，同时发送 HTTP
`session-id` 请求头和 JSON `prompt_cache_key`。它取自 Thread ID，因此多步调用、
`for_step()` 克隆和 `resume()` 不会重置标识；独立 Thread 使用不同标识。
非持久 Session 使用其运行期 ID；直接调用 `ChatGPTResponsesClient` 时，适配器为
该客户端生成并保留一个标识，克隆时沿用。

这仅提供缓存路由亲和性，不能保证命中；提示词前缀、工具定义和模型设置仍需匹配。
继续使用 `store=false`，不自动发送 `prompt_cache_retention` 或
`prompt_cache_options`。其他供应商的 Responses 适配器不会仅因 Prompt 携带会话提示
而发送这些字段或请求头。缓存命中也不等同于 ChatGPT 订阅额度按相同比例节省。
参见 [OpenAI 的会话缓存路由说明](https://github.com/openai/codex/pull/44862)
及 [缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching)。

管理员以 root 运行并使用已有宿主登录时，凭据替换保留原文件的属主，新凭据文件及锁文件沿用凭据目录属主，权限仍为 0600。普通宿主进程可以继续读取及串行续期；不会因 root 续期把共享登录改成仅 root 可读。

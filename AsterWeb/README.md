# Aster Web

独立的网页检索 MCP。提供统一的 `web_search` 与 `web_fetch`，每次明确选择 `exa` 或 `deepseek`，不调用辅助推理模型，不自动切换供应商。不依赖 Aster Harness、Squad 或 Relay。

```sh
python -m pip install .
# stdio：由任意 MCP 客户端启动
aster-web
# HTTP：供多个客户端使用，默认只监听本机
aster-web --transport http --port 19930
```

HTTP 的端点为 `/mcp`，启动前设置至少 24 字符的 `ASTER_WEB_TOKEN`，客户端通过 Bearer 认证。stdio 由启动进程的宿主管理访问权限。设置 `DEEPSEEK_API_KEY` 才能使用 DeepSeek；Exa 不设 `EXA_API_KEY` 时使用官方 MCP 匿名限流入口，设 key 后使用该账号额度。凭据由宿主环境提供，不写进项目、工具参数或网页返回值。

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `web_search` | query、必填 provider、limit=5（1–10） | provider、query、results、truncated；条目含 title/url/summary，可选 published_at |
| `web_fetch` | url、必填 provider、max_characters=5000（100–20000） | provider、url、title、content、truncated |

query 限制 1–128 字符。错误是 MCP 工具错误，正文为 JSON，带实际供应商及 code、upstream_status、retryable，不返回供应商原始错误或认证内容。服务不自行重试；DeepSeek 有内容的成功请求会产生费用，读结果的 agent 还会消耗自身模型 token。

DeepSeek 适配直接调用官方 Search/Fetch JSON API。Exa 适配连接官方远程 MCP；其匿名入口返回格式化文本，适配器解析来源元数据并限制输出，格式变化时明确报错而不是伪造结果。搜索摘要每条最多 1500 字符。网页资料和服务返回内容均不可信，`site:` 不保证严格过滤，需检查实际 URL。

抓取由供应商执行，服务不直接请求用户给出的网址；拒绝带凭据、明显内网和非 HTTP/HTTPS 地址。供应商仍负责其抓取网络的访问策略。这里不提供浏览器登录、点击或视频理解。匿名服务限额和供应商覆盖可能变化。

附带的 [web skill](aster_web/assets/skills/web/SKILL.md) 可由宿主复制或注册到自己的 skill 目录；不要放进 agent 的私人 workspace。客户端仅需标准 MCP 接入，不要求使用本项目的命令名称或某一种模型。

```sh
python -m unittest discover -s tests -v
```

测试使用本机假后端，不调用收费搜索。暂未指定许可证。

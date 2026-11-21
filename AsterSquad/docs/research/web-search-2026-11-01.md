# 搜索服务与 Codex 搜索复用调研

调研日期：2026-11-01。结论区分官方文档、小规模实验和建议。价格是当日公开标价，调用量、模式和正文处理另计，不代表未来报价或质量排名。

## 结论

先使用独立的搜索与网页读取 MCP，供应商不写进 Harness 核心。Exa 的匿名 MCP 已通过小规模查询与正文读取实验，可作为首个候选；长期多人使用应评估具名账号、限额与费用。Tavily、Brave、Firecrawl 都有官方 MCP。SearXNG 是自托管备选，带来额外运维。

调研后实现了独立 Aster Web，见文末接入说明。Exa/Tavily 实验使用匿名入口，DeepSeek 使用 API key 认证；实验直接调用检索服务，不调用推理模型。

## Codex 能否单独拿出搜索

1. Codex 的内置网页搜索是托管服务，不是开源 CLI 附带的本地搜索引擎。OpenAI 明确区分开源 Harness、接口层与另外提供的模型和托管服务。复制 CLI 的调用代码不等于获得可独立部署的搜索后端。[平台说明](https://developers.openai.com/blog/codex-as-a-platform)
2. 没有查到官方发布的通用网页搜索 MCP。公开的 [OpenAI Docs MCP](https://developers.openai.com/learn/docs-mcp) 仅提供 OpenAI 文档搜索和读取。
3. 官方目前有 `standalone_web_search` 方向，但仍在开发、默认关闭，需要运行时、供应商和模型同时支持；不能仅加 `supports_standalone_web_search=true` 就视为可用的公开搜索 API。[搜索配置说明](https://learn.chatgpt.com/docs/web-search)
4. 官方旧 Cookbook 曾介绍 `codex mcp-server`；[当前命令文档](https://learn.chatgpt.com/docs/developer-commands) 明确说该命令及独立二进制已移除，改用 app-server。此前讨论中的直接 MCP 委托方案需要据此更正。

验证版本：Codex CLI 0.160.0；`codex help mcp-server` 返回不认识该命令；feature 列表中 `standalone_web_search` 为 under development / false。通过 `codex app-server generate-json-schema --experimental` 生成的 167 个客户端方法里，没有发现可直接发起独立网页搜索的方法。schema 内有搜索事件、结构化结果，并不等于存在独立搜索入口。该结论只适用于本次检查的版本。

仍有两条可实现的复用路线：

| 路线 | 实际含义 | 取舍 |
| --- | --- | --- |
| OpenAI Responses API + `web_search` | 调用支持该工具的 OpenAI 模型；搜索由服务端执行，返回回答、调用记录和来源 | 是公开支持的接入方式，但不是原始搜索结果 REST 接口；包装成 MCP 后 DeepSeek 也能调用，需要另一次 OpenAI 模型处理 |
| Codex SDK / exec / app-server 委托 | 自建小型适配，让一个启用搜索的 Codex 完成只读检索任务 | 复用完整 agent，不是提取一个搜索函数；有额外进程、推理及额度开销，尚未在本轮实测 |

[Responses 搜索文档](https://developers.openai.com/api/docs/guides/tools-web-search)列有 search、open_page、find_in_page。公开[价格](https://developers.openai.com/api/docs/pricing)为普通 web search $10/1,000 次工具调用，另计搜索内容及模型 token。API 认证、账号权限和可用额度应单独核对，不能从当前 ChatGPT 登录能聊天推导搜索接口可用。未尝试复制登录凭据调用未公开端点，也未开启实验功能。

## 现成服务

| 服务 | 接入与能力 | 标价与试用 | 本轮验证 |
| --- | --- | --- | --- |
| Exa | 官方远程 MCP；搜索、已知 URL 正文读取 | 免 key 限流试用；账号每月 $10 免费余额。Lite 搜索 $4/千次，Auto/Fast $7/千次，Contents $1/千页，额外结果/摘要另计 | 英文查询、中文查询、正文读取均成功 |
| DeepSeek | 官方独立 Search / Fetch JSON API；可通过 MCP 或脚本工具适配，搜索结果不依赖推理模型 | Search、Fetch 分别 5 元/千次成功且有结果的请求；与模型 token 分开 | 已做少量英文及境外站点对照，见文末；这些样本 Exa 整体更符合预期 |
| Tavily | 官方远程 MCP；search/extract/crawl/map/research，支持 OAuth、API key 与匿名模式 | 账号每月 1,000 credits；按量 $0.008/credit。Basic 搜索 1 credit，Advanced 2 credits；正文提取另计 | 能连接和列工具；匿名搜索返回月度限额业务错误，没有取得搜索结果 |
| Brave | 官方开源 MCP，可运行 stdio 或 HTTP；网页、新闻、图片、LLM Context 等 | Search $5/千次，套餐表列每月 $5 免费 credits；需要账号/API key，页面有更高免费额宣传，实际以账号方案为准 | 仅核对文档与官方 MCP 仓库，未注册账号或付费实测 |
| Firecrawl | 官方 MCP；搜索、抓取、解析，另有更完整的页面处理能力 | 文档提供匿名限流的 Search/Scrape/Parse；账号套餐另计，本轮不引用未核实的付费单价 | 仅核对文档，未实测 |
| SearXNG | 开源元搜索，可自托管 HTTP JSON API，再接 MCP | 软件开源；服务器、网络、上游引擎限制和维护仍有成本 | 仅核对文档；公共实例不保证开放 JSON，不作为默认稳定入口 |

官方来源：

- Exa：[MCP 与认证](https://exa.ai/docs/get-started/exa-mcp)、[价格](https://exa.ai/docs/admin/pricing)。
- DeepSeek：[2026-10-17 开放公告](https://api-docs.deepseek.com/zh-cn/updates/)、[Search / Fetch 指南及计费](https://api-docs.deepseek.com/zh-cn/guides/search/)。
- Tavily：[MCP](https://docs.tavily.com/documentation/mcp)、[免 key 模式](https://docs.tavily.com/documentation/keyless)、[Credits 与价格](https://docs.tavily.com/documentation/api-credits)。
- Brave：[API 与价格](https://brave.com/search/api/)、[官方 MCP](https://github.com/brave/brave-search-mcp-server)。
- Firecrawl：[MCP](https://docs.firecrawl.dev/mcp-server)、[匿名与 API key 接入](https://docs.firecrawl.dev/mcp-server/keyless)。
- SearXNG：[搜索 API](https://docs.searxng.org/dev/search_api.html)、[项目](https://searxng.org/)。

## 小规模验证记录

使用独立 MCP 客户端验证连接和返回格式；查询使用公开技术资料。

- Exa `https://mcp.exa.ai/mcp` 实际列出 `web_search_exa`、`web_fetch_exa`。
- 英文查询 `Python pathlib Path official documentation` 返回 Python 官方文档，单次工具调用约 3.3 秒；这是单次观察，不是延迟基准。
- 中文查询 `Python pathlib 官方文档 路径操作` 返回英文与简体中文官方文档，也有第三方镜像；不能把每条结果都当成官方来源。
- 使用 `web_fetch_exa` 读取官方 pathlib 页面成功。实际 schema 支持 `urls` 和 `maxCharacters`，默认每页 3,000 字符。工具参数以服务当前 schema 为准。
- Tavily `https://mcp.tavily.com/mcp/`，请求头 `X-Tavily-Access-Mode: keyless`：初始化和工具列表成功，但查询返回 `monthly_cap_reached_bonus_eligible`。MCP `isError=false` 仍可能包含业务失败，不能只看传输成功或该字段。
- 没有按服务返回的付款、问卷换额度等提示继续操作。匿名限额原因和精确额度范围未确定，不能推断为用户账号额度不足。

## 设计建议（调研阶段）

1. 先补 Squad 的通用 MCP 配置与持久化入口，复用 Harness 已有的 Streamable HTTP、请求头/环境凭据及运行时更新能力。不要在 worker 中硬编码第三家搜索供应商。
2. 首个候选连接 Exa，仅使用搜索和 URL 正文读取；Tavily 的具名免费账号可作为后续对照。无需先自建搜索引擎或多供应商聚合网关。
3. 使用简短 skill 指导查询、核对原文与引用；先取少量结果，再按需读正文。限制正文长度，保留来源链接与发布时间，沿用 Harness 输出预算，避免一次注入大量网页。
4. 免费匿名入口先做有限试用；多人持续使用前核对服务限额、错误语义、具名账号与支出上限。不要从单条查询推断整体中文质量或新闻时效。
5. 验收覆盖英文技术资料、中文来源、近期信息、无结果、限流和长页面；先少量检查，不以反复大规模模型测试代替服务验证。

## 补充：DeepSeek 独立搜索 API

Exa 与 DeepSeek 均作为独立检索供应商评估。官方更新日志确认独立搜索 API 于 2026-10-17 开放，不能沿用早期“只有网页端支持搜索”的旧信息。

公开端点为 `POST https://api.deepseek.com/v1/tools/search` 与 `POST https://api.deepseek.com/v1/tools/fetch`，使用 DeepSeek API key 的 Bearer 认证。Search 返回结构化标题、URL、摘要、可用日期及评分，可选正文；Fetch 返回 Markdown。[Search 参考](https://api-docs.deepseek.com/zh-cn/api/search/)、[Fetch 参考](https://api-docs.deepseek.com/zh-cn/api/fetch/)。

官方 Harness 的 [web-deepseek-api 适配说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-deepseek-api/README.zh.md) 明确每次操作为直接 API 请求，无需辅助模型。因此可以给 GPT、DeepSeek 或其他模型提供同一检索能力，不必套一层 DeepSeek 推理。

[官方计费说明](https://api-docs.deepseek.com/zh-cn/guides/search/)：Search / Fetch 各 5 元/千次；成功且有内容才收费，结果条数不改变单次价格。一次搜索加一次抓取合计 0.01 元，不包含消费结果的 agent 模型 token。此前服务端工具调用的免计费期于北京时间 2026-11-01 00:00 结束，不应继续按旧的免费说明估算。

官方文档主要提供 HTTP API；已有社区 MCP 项目，但未做代码与运行验证，也未确认官方独立搜索 MCP。接入时应区分直接 Search / Fetch 封装和先调用模型合成回答的旧封装。API 实际可用性和样本结果如下。

## 英文与境外站点小样本对照

实验条件：DeepSeek 使用官方独立 API，`count=3, need_content=false`；Exa 使用匿名 MCP 的默认 `web_search_exa`，`numResults=3`。查询相同，没有使用 Exa 高级域名过滤。另对四个完全相同的 URL 做正文读取；Exa 限制每页 3,000 字符。没有调用推理模型。

| 样本 | DeepSeek | Exa |
| --- | --- | --- |
| 英文 pathlib 官方资料 | 找到官方文档，但三条里混有中文、西班牙语版本 | 找到英文官方文档、源码和 PEP |
| 英文 NASA 近况 | 默认返回西语、越语及英语聚合页；加 `lang:en site:nasa.gov` 后返回一份 2023 年研究论文，仍不是近期更新 | 默认和限定查询均返回 NASA 官方更新页面 |
| YouTube NASA 视频 | 带路径和纯域名两种 `site:` 查询均为空；所测视频 Fetch 404 | 两种查询都返回视频链接，读取该页面得到文字片段，不代表已验证完整字幕或视频理解 |
| Reddit LocalLLaMA / DeepSeek | 带路径和纯域名两种限定均为空；所测帖子 Fetch 404 | 返回相关帖子，并读取到帖子和评论文字 |
| X / OpenAI | 返回站外 Box 模型资料，未满足原站限定 | 返回 OpenAI 官网与第三方 X 镜像，也未满足原站限定 |
| 普通英文维基 Python 页面 | 搜索能找到维基 PDF 及相关页面；直接 Fetch 该条目 404 | 找到相关条目，直接读取 Python 条目成功 |
| 英文维基 1989 年天安门事件 | 搜索为空；所测条目 Fetch 404 | 找到对应条目，正文读取成功 |

查询包括 `Python pathlib Path official documentation`、`NASA Artemis II latest mission update`、`NASA Artemis launch site:youtube.com`、`DeepSeek LocalLLaMA site:reddit.com`、`site:x.com OpenAI GPT-5`、`Python programming language site:en.wikipedia.org`、`site:en.wikipedia.org 1989 Tiananmen Square protests`；NASA 追加 `lang:en site:nasa.gov` 复核。最初 YouTube/Reddit 查询还指定了路径，因此另用纯域名复查，未只依据最初空结果下结论。

DeepSeek 共 10 次 Search（全部 HTTP 200，其中 5 次非空）及 4 次 Fetch（均 404）；按官方规则预计计费 0.025 元，实际以账单为准。Exa 对相同搜索词和 URL 做对照，搜索各返回三条，但 X 的站外结果没有被算作原站检索成功。返回有内容也不等于信息准确、实时或全面。

结论只适用于这组查询与默认配置：英文普通资料能用，但 DeepSeek 在本次境外平台和英文近况样本中表现较弱；当前优先 Exa。不能仅凭空结果或 404 判断原因是审查、防火墙、索引覆盖、重定向、站点反爬还是其他抓取限制；也不能由一个政治历史查询概括全部主题。搜索或正文读取成功不代表调用者的浏览器可以直连对应网站。


## 接入说明

2026-11-01：独立项目 [Aster Web](https://github.com/WayneW1119/AsterWeb) 提供标准 stdio / Streamable HTTP MCP。`web_search` 与 `web_fetch` 均显式选择 `provider=exa|deepseek`，有统一输出长度与错误格式，不自动重试、切换或调用辅助模型。服务本身不依赖 Harness、Squad、Relay。

Squad 0.4.11 增加宿主 MCP 服务目录，创建/编辑成员可勾选服务 ID，凭据只在宿主引用。目录支持标准 MCP 配置，不硬编码搜索供应商；配套 web skill 放在共享 skill 目录。Harness 0.1.7 无需修改。

供应商连接成功不保证任意页面都能抓取。调用者应处理空结果、限流与抓取超时；服务明确返回错误，不隐式切换供应商或自动重试。

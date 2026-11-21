# MCP 工具发现、WorldState 与压缩

实现日期：2026-10-24。参考上游 Codex 提交
`c419fd01a70dd1d05356da443ac44328bda2e6f4`，默认工具英文说明与参数见
[完整定义对照](../reference/codex-mcp-tool-definitions.md)。本实现保留 Python、事件驱动和
每 Step 捕获请求的设计，没有引入 turn。

## 请求与执行

1. MCP 服务在首次运行 Session 时连接，分页发现并应用原有工具过滤。
2. 初始化返回的 instructions 保存在连接上，用作命名空间说明和搜索元数据。
3. MCP 业务工具注册到内部路由，但不直接出现在顶层 tools。
4. 顶层提供 Codex 同形的 `tool_search`：`execution=client`、query、limit（默认 8）。
   SEARCH_SPEC 的内部 name 仅用于注册，Prompt/API 定义不包含这个额外字段。
5. 模型返回 `tool_search_call`；客户端用 BM25 检索名称、服务、说明和参数名称，合并命中
   定义为 namespace。这里只使用标准库实现 BM25，不运行网络 embedding；分词和排名
   不承诺与 Rust 的 bm25 库逐字节一致。
6. `tool_search_output` 经普通事件队列，下一 Step 可见。它包含正式 schema 和
   defer_loading 标记，不是描述工具名称的普通文字。
7. 模型发出带 namespace 的 function_call；路由绑定精确 MCP 客户端和原始工具名。
   Step 捕获的已加载定义必须与当前绑定一致。未加载、已移除或变更的定义返回要求重新
   搜索的错误，不悄悄执行不同 schema 的动作。内部旧平面路由键仍可供兼容调用使用。

工具搜索结果与普通调用的 call_id 都做唯一性、持久化配对、排队回执和压缩范围检查。
传输层也对两种调用执行既有的“回执先于插话消息”投影，不改档案的事件顺序。
无 MCP 配置时不添加这些工具、状态或上下文，原有行为契约不变。

## 来源列表交给 WorldState

`ToolsState` 只提供延迟命名空间和简要用途；工具搜索 description 不重复来源列表。
来源从相同的 binding 得到，取 instructions 首行（至多 250 字符），总列表保留约 4 KiB
预算，优先保留名称，再分配说明空间。该摘要不是完整服务 instructions。

当前来源有固定上下文基线，首次连接后固定；后续变化作为 developer `<tools>` 消息追加，
使用 Codex 的 Deferred / Added / Removed tool namespaces 标题。无变化时不重复消息。
完整服务说明随匹配工具的命名空间定义提供。宿主现在可以在运行中提交完整的 MCP 服务
配置替换，下一 Step 边界实际新增、修改或移除来源；同 Thread resume 也恢复最新配置。
尚未监听服务端 tools/list_changed 通知。

内部消息的 `_mcp_sources` 记录精确的来源状态；只有运行时能产生，准备请求时移除，
不会作为额外 API 字段发送。模型输出不能伪造此标记或 `_mcp_searches` 检查点。

## 资源工具

已连接的服务另外通过三个通用函数访问：

- list_mcp_resources：按服务／游标列出资源；不指定服务时汇总各服务的所有页。
- list_mcp_resource_templates：同样列出参数化 URI 模板。
- read_mcp_resource：按服务名称和具体 URI 读取内容。

使用 Codex 默认的英文描述、strict=false、必填字段和额外参数限制；不接入模型目录覆盖。
Codex 同形的 JSON 回执包含 server、resources／resourceTemplates／contents 等字段。
URI、二进制 blob 等 SDK 类型以 JSON 模式序列化，避免 Python AnyUrl 无法编码的问题。
分页检测循环并有页数／总条目限制；未知服务、无效游标、读取失败均产生普通错误回执。
资源列表不用于发现工具；未声明 resources 能力的服务列出空目录。

## compact：来源保留，搜索定义随历史移除

当前规则更新于 2026-10-26。WorldState 只持续呈现来源简介；原生
`tool_search_call` / `tool_search_output` 与普通历史遵循相同的压缩范围规则。

- 局部 compact：删除覆盖的搜索调用／结果，不生成定义检查点。未覆盖的搜索仍有效。
- 整体 compact：重建来源基线，不搬运已发现定义；需要调用时重新搜索。
- 已加载定义仅从当前有效历史的真实搜索对重建。摘要、归档查询的文字和旧检查点均不激活工具。
- 实际 MCP 连接、自定义脚本注册及来源仍存在；移除定义不等于断开服务或删除工具。
- 搜索调用／回执仍须配对，不能压缩尚无回执的搜索。

请求前继续验证重建值与跟踪状态相等，防止模型不可见的定义仍通过执行检查。
来源更新仍采用固定基线和必要的位置检查点，因为来源属于 WorldState；这不保留搜索定义。

`mcp_context` 投影格式为 v2，固定基线的 `searches` 兼容槽始终为空，`current.searches`
只是当前有效历史搜索对的可重建索引。包含工具状态的压缩操作继续使用既有 v3 格式；
同时接入扩展 WorldState 部分时可用通用 v4 格式。

恢复旧 v1 投影时，先按旧规则验证存档证据，再清除旧基线里的定义及旧 `_mcp_searches`
检查点的激活作用，按仍然可见的原生搜索对重建状态，并追加 v2 状态记录。旧检查点不再
进入模型请求，但不修改或删除原始归档。迁移不是重新搜索或重放业务调用。
排队但尚未消费的搜索结果仍原样恢复；消费后才加载一次，不提前加载、不重放。

## 验证

`tests/test_mcp_discovery.py` 覆盖原生搜索与命名空间调用、资源和模板读取、分页、错误、
来源差异、可见证据缺失、过期定义阻止执行、局部／嵌套／整段压缩、排队结果恢复及
同 Thread resume。`tests/test_mcp.py` 保留真实 SDK 进程的生命周期与错误测试。

`scripts/mcp_discovery_live_check.py --live --tools-from PATH` 使用已单独安装的 Filesystem
服务与 SDK 测试资源服务运行真实模型任务，实例和运行产物使用隔离测试目录，不随项目分发。
完整请求、响应、工具和压缩记录保存为 JSONL；使用独立测试 Thread。

## 运行中替换服务配置（宿主 API）

```python
await session.set_mcp_servers({
    "analytics": {"command": "/path/to/python", "args": ["/path/to/server.py"], "required": True}
})
```

这是完整替换：未列出的服务会移除。提交返回 event_id，表示请求已入队，不代表连接成功。
请求在下一个 Step 边界生效；当前模型请求和工具批次继续使用原绑定，不被中途切换。
未变化的服务复用客户端；新增或配置变化的服务先启动并校验目录，成功后再切换绑定、
关闭退役连接。必需服务启动失败时保留旧绑定和配置，产生失败事件，不停止普通任务。

已应用的配置作为 mcp_configuration 记录持久化；队列中的请求包含内部配置证据，因此
正常 suspend/resume 不丢失尚未应用的更新。同次队列排空中的多个完整替换请求以最后
一个为准。内部配置字段不进入模型请求，配置变更不是发布等业务动作的用户授权。

这不是模型管理工具，也未添加手机 UI 或 HTTP 配置接口。实际服务配置由宿主控制。
支持 stdio 和 Streamable HTTP，认证采用 Token/请求头。不包含 OAuth、Apps/Plugins/Skills 管理、服务端目录变更通知、sampling 或 elicitation。

运行时边界测试见 tests/test_mcp_runtime.py；长任务脚本为
scripts/mcp_worldstate_long_check.py。scripts/mcp_trace_report.py 可以将完整 API 输入输出
按公共定义去重，生成 process.md/process.json，并逐条还原校验；expand 子命令可展开。

# 通用工具命名空间

namespace 是模型工具的分组及精确路由机制，不表示工具必须来自 MCP。
普通宿主工具、MCP 服务工具和 Agent 自定义脚本工具共用 ToolRegistry 的这一层。
本文记录通用 namespace；没有将所有现有内置工具改名。后续已接入可选 [Code Mode](code-mode.md)，通过 functions namespace 提供 exec/wait。

## 注册与声明

```python
registry.register_namespace("functions", "基础工具", [Tool(spec, handler)])

# 可在同一个组中增加成员，省略说明时沿用已经注册的组说明。
registry.register(another_tool, namespace="functions")
```

register_namespace 接收一个组的说明和一批 Tool，整批验证后发布；重复成员或冲突说明会拒绝，
不会先留下半批成员。namespace 与成员名称允许 1–64 个 ASCII 字母、数字、下划线或连字符。
同一名称可以在不同的非默认 namespace 中存在，也可以与默认工具同名。
`functions` 是默认 namespace：省略、空字符串与显式 `functions` 归一到同一身份。
顶层 `echo` 与 `functions.echo` 不能重复注册；两个方向以及整批注册都会检查冲突。
组说明需要改变时，先构造替代 registry，再在 Step 边界发布，不修改正在执行的快照。

模型声明：

```json
{
  "type": "namespace",
  "name": "functions",
  "description": "基础工具",
  "tools": [
    {
      "type": "function",
      "name": "example",
      "description": "示例工具",
      "parameters": {"type": "object", "properties": {}, "additionalProperties": false},
      "strict": false
    }
  ]
}
```

模型调用传入独立的 namespace 和 name：

```json
{
  "type": "function_call",
  "namespace": "functions",
  "name": "example",
  "call_id": "call-1",
  "arguments": "{}"
}
```

内部先将省略或为空的 namespace 归一为 `functions`，再按 (namespace, name) 的逻辑身份查找，不拼接为一个字符串。
因此 a__b.c 与 a.b__c 不会串组。省略 namespace 只查默认组，不会回退到其他同名组。
默认组中的工具无论最初以顶层还是显式组形式注册，调用时都支持省略、空字符串和 `functions`。
为保持普通 Responses 请求兼容，顶层注册继续输出顶层定义，显式分组继续输出 namespace；内部身份相同并不要求改变这两种声明形式。
默认组的延迟工具同样需要搜索加载，不能靠省略 namespace 绕过检查。
旧的扁平工具继续可用，MCP 的旧扁平路由由显式、不可见的兼容 alias 提供；
不会为所有 namespace 自动生成扁平 alias，自定义工具仍要求正确 namespace 与加载证据。

## 直接提供与延迟加载

Tool.deferred=False 的成员通过 Prompt.tools 中的 namespace 声明直接提供。
Tool.deferred=True 的成员不在顶层声明中枚举：

1. 原生宿主 namespace 可以使用 registry.deferred_definitions 提供搜索元数据。
2. Session 将这些定义与 MCP、自定义脚本定义合并成同一个 DeferredBinding。
3. 来源摘要进入同一个 WorldState tools 节，搜索入口只有一个 tool_search。
4. 搜索返回实际 namespace/function 定义，后续调用必须与当前 registry 中的定义一致。
5. 直接可见的原生 namespace 不会被误当作必须搜索的 MCP 服务。

一个组可以同时有直接成员和延迟成员。空组不生成无成员的 API namespace 声明。
特殊原生 tool_search 仍作为独立协议工具提供，不包装成普通 namespace function。
当前通用 namespace 成员支持 function；可选 Code Mode 使用 JSON function 入口，自由文本 custom grammar 仍未实现。

## 与现有自定义工具组结合

自定义工具的持久化组仍由 CustomToolManager 管理，保留描述、详细说明、组版本和脚本版本。
它将 local_<group> 及成员交给同一个 register(namespace=...)，不再靠拼接名称实现另一套路由。
MCP 同样将 mcp__<server> 及其成员交给这一层。

```text
普通宿主 namespace ─┐
MCP 服务工具 ──────┼─ ToolRegistry namespace ─ Step 快照 ─ ToolRouter ─ 原有执行器
自定义脚本组 ──────┘
```

ToolRouter 对全部延迟成员使用统一的已加载定义检查；不再需要按工具是否来自 MCP 决定如何路由。
MCP 客户端、自定义脚本运行器仍各自负责实际执行，分组不会改变它们的权限或进程环境。
注册表的 copy/snapshot 同时复制组说明、成员定义和 alias，保留 handler 的请求时绑定。
分组不改变工具回执排队、发送消息、等待、事件发生器或 compact 的语义。
显式 `functions.write_memory` 与顶层记忆调用使用相同的版本提交与历史证据校验，compact/resume 也识别这两种调用。
Code Mode 的默认组成员使用 `tools.<name>`，其他组使用 `tools.<namespace>__<name>`；默认组身份归一不会让记忆写入或上下文管理工具进入脚本目录。

自定义组继续按既有方案持久化并正常恢复；原生宿主组由宿主代码提供，
不会把任意 Python handler/lambda 序列化成可恢复代码。

## additional_tools 与 WorldState

两者不是同一个概念，但 Codex 可以把它们连接起来：

- additional_tools 是携带真实可调用定义的输入项格式。
- WorldState 是跟踪状态、计算变化并生成上下文更新的机制。
- Codex 的 Responses Lite 可在请求前缀中放 additional_tools。
- 启用增量工具模式时，TopLevelToolsState 也会由 WorldState 生成 additional_tools，
  只追加新增/修改的定义，并报告移除或组说明更新。

因此，“additional_tools 不是 WorldState”只能说明它们概念不同，不能推导为 WorldState 不会生成它。
我们当前 WorldState 保存来源摘要；已加载定义仅由有效历史中的原生 tool_search_output 提供。compact 覆盖后即移除，需要时重新搜索，不额外保留定义检查点。
本次通用 namespace 继续使用普通 Responses tools / native search，不新增 Responses Lite 的 additional_tools 传输。

参考源码：codex-rs/tools/src/responses_api.rs、core/src/client.rs、
core/src/context/world_state/top_level_tools.rs、core/src/session/world_state.rs。

## 验证

tests/test_tool_namespaces.py 覆盖同名路由、非拼接身份、组描述、原子注册、快照、兼容 alias、
直接与延迟混合成员，以及共用搜索、WorldState、compact。
现有 MCP、自定义工具和旧行为合同回归继续执行。
真实模型探针使用两个同名原生工具及一个自定义脚本组，验证实际 namespace 声明与精确调用。


默认 namespace 对齐另覆盖：两种注册形式的三种调用拼写、双向重复和原子性、旧 alias 与快照、默认延迟成员的搜索检查、显式默认记忆调用的 compact/resume，以及省略 namespace 调用 Code Mode。参考上游 Codex c419fd0 的 protocol/src/tool_name.rs、core/src/tools/router.rs 和 tools/src/tool_spec.rs。没有新增 Responses Lite 传输。

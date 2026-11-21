# 自定义脚本工具

Agent 可以编写脚本，并通过 `manage_tool_group` 和 `manage_custom_tool` 将其注册为模型可调用的工具。
参考 Codex DynamicToolSpec / DynamicToolHandler 的定义与执行分离：保存描述和 JSON Schema，
运行时绑定具体执行器。这里额外提供模型侧管理入口，变更在事件驱动的 Step 边界应用；没有引入 turn。
不启动 MCP 服务、不实现 MCP 协议，也不把脚本导入 Harness 的 Python 进程。

## 模型侧接口

`manage_tool_group`：

- `create` / `update`：传入 group、description、可选 instructions；update 完整替换两段说明。
- `list`：列出组摘要、版本和成员名称。
- `get`：查询一个组的完整活动定义。
- `remove`：移除组及其全部工具。

`manage_custom_tool`：

- `register` / `update`：传入 group、name 和完整 definition。
- `list`：列出组内工具的描述和版本。
- `get`：查询一个工具的完整活动定义。
- `remove`：移除一个工具。

两类管理工具始终可见。`tool_search` 也从初始请求就存在，即使目录尚为空，避免自行扩展时再新增搜索入口。
组和工具名称以字母开头，允许 ASCII 字母、数字、下划线和连字符，最长 40 字符。
自定义组使用 `local_<group>` 命名空间，与 `mcp__<server>` 分开。

definition 示例：

```json
{
  "description": "对整数列表求和。",
  "input_schema": {
    "type": "object",
    "properties": {
      "numbers": {"type": "array", "items": {"type": "integer"}}
    },
    "required": ["numbers"],
    "additionalProperties": false
  },
  "execution": {
    "command": "python3",
    "script_path": "scripts/sum.py",
    "args": [],
    "cwd": ".",
    "timeout_sec": 30
  }
}
```

先用现有 apply_patch / 命令工具编写、修改脚本，再显式 register/update。
script_path 必须是工作区内的文件，cwd 必须在工作区内；解释器通过可执行文件路径或 PATH 解析。
输入 schema 使用 Draft 2020-12，必须为 object；仅允许本地 `$ref` / `$dynamicRef`，不联网解析 schema。
依赖 `jsonschema`，由基础安装提供。最多 64 个组、256 个工具；每个入口脚本最多 1 MiB。

## 注册与版本

管理调用先验证配置并复制入口脚本，随后排队一个变更请求并返回 accepted / request_id。
下一 Step 在记录该批输入、生成 WorldState 和请求模型之前，按接受顺序应用变更。
应用结果作为 custom_tool_change_applied 或 custom_tool_change_failed 事件进入队列，带同一 request_id。
accepted 不等于已启用。查询只返回活动状态，不把待应用请求当作成功。
同一批创建组和注册工具可以依次应用；某一请求失败不撤销前面已成功的请求。

变更写入 registry.json 的临时文件，再原子替换；失败不发布新状态。
当前模型请求和整个工具批次使用捕获的旧绑定，不会在批次中途切换实现。
入口脚本副本有 SHA-256，调用前校验。修改原脚本不会改变活动版本。
更新工具或组后，版本信息进入具体工具描述，旧加载定义会被拒绝，必须重新搜索。
组的变更会使该组所有成员的加载定义失效；移除后历史中的旧定义不能重新执行。
自定义工具只能通过 namespace 调用，不能用拼接后的内部路由名称绕过加载检查。

只复制入口脚本，不复制全部依赖。其他文件、模块、解释器、外部服务等仍是活动依赖；
使用 cwd / 显式路径组织数据和依赖，不假设入口脚本仍在原文件夹。

## 执行与事件

执行 argv 为 `[command, copied_script_path, *args]`，不经过 shell：

- stdin：一个 JSON 参数对象加换行，最多 1 MiB，随后关闭输入。
- stdout：工具输出，可为 JSON 或文本。
- stderr：诊断日志。
- 返回：group、tool、version、success、exit_code、timed_out、stdout/stderr 和截断标记。

参数先按 schema 校验，失败不启动脚本。stdout/stderr 各保留 64 KiB，同时持续排空管道防止死锁。
timeout_sec 为 1–300 秒，默认 30。超时或取消清理拥有的进程组；没有自动重试。
普通工具回执默认唤醒后续 Step。脚本没有发生器 IPC，不能假装受管理的事件发生器；
持续监听仍使用 register_program_generator。脚本不是沙箱，使用现有宿主权限，包装不会增加隔离或权限。
超时可能已经产生副作用，outcome_unknown 提醒显式核验后再考虑重试。

## WorldState、搜索与压缩

工具组来源摘要进入现有 WorldState tools 节，包含简短说明、组版本和工具数量。
初始基线固定，之后以 Added/Removed 更新追加到历史，不反复改写早期上下文。
空组也会出现在来源摘要中；组的详细 instructions 在搜索返回的 namespace 描述中提供。
自定义工具与 MCP 共用 BM25 搜索目录及原生 tool_search_call / tool_search_output。
初始请求 tools 不枚举业务工具；加载的完整定义存在原生搜索回执中，而不是只存在 WorldState 摘要里。

来源摘要复用现有 WorldState 基线与来源检查点。具体搜索定义仅属于有效历史，
compact 覆盖后即移除；需要时重新搜索，实际工具注册和脚本副本仍保留。
内部 McpProjection、mcp_context 和 _mcp_* 名称保留以兼容已有 rollout，语义已包含两类延迟工具。
不把旧调用重放为新调用，不编造加载结果。

## 保存与恢复

持久 Thread 的目录：

```text
<thread>/custom_tools/registry.json
<thread>/custom_tools/revisions/<id>/<entrypoint>
```

每次成功变更同时记录 custom_tool_state；resume 核验文件中的注册表与保存状态一致，
并校验脚本副本。恢复定义和绑定，不重放调用。排队但尚未应用的注册请求随正常暂停保存，
其中引用的入口副本也已经创建。内存 Session 使用 Session 独有目录，不跨 Session 自动恢复。
不支持任意崩溃恢复、通知式热加载或跨 Thread 自动共享。

## 验证

`tests/test_custom_tools.py` 使用真实子进程验证完整模型工具链、版本隔离、失败更新、
参数校验、删除、压缩、恢复、超时及输出上限。
`scripts/custom_tools_live_check.py --live` 使用真实模型创建脚本、分组、注册、搜索、调用、
修改和更新，再经宿主明确执行的 whole compact 与正常 resume，重新搜索所需定义后调用工具。
输出完整原始请求回复，以及去重但可无损还原的 process.json / process.md / process.html。

# Code Mode：事件驱动的 JavaScript 工具编排

当前实现基础版 `functions.exec` / `functions.wait`，以及脚本内的 `text`、`image`、`store`、`load` 和 `ALL_TOOLS`。这是现有工具的编排入口，没有新增 Agent loop 或 turn，也没有将所有原有工具隐藏或改名。

## 启用

```bash
python3 -m pip install '.[code-mode]'
python3 -m aster --workspace ./workspace --code-mode
```

嵌入宿主使用 `SessionConfiguration(code_mode_enabled=True)`。默认关闭，已有 Thread 配置缺少字段时仍为关闭；开启后的 Thread 保存该配置，resume 不必重复传参。CLI 的 `--code-mode` 用于新 Thread。运行时依赖 `mini-racer>=0.14.1,<0.15`，普通核心导入和关闭状态不加载 V8 库。

与 Codex 的分层对应：运行时管理 isolate 和 cell，工具桥接层执行已有工具，Session 管理生命周期。本项目使用 Python＋V8，不是 Codex Rust 实现的逐行移植。第一版通过 namespace 中的 JSON function 接收 `code` 字符串，未实现自由文本 grammar、Responses Lite 或 TypeScript 执行。

## 两个模型工具

`functions.exec(code, yield_time_ms=1000, timeout_ms=30000)` 接收 JavaScript，支持顶层 await。短脚本返回 completed；长脚本返回 running、cell_id 和已有输出，之后可读取增量结果。

`functions.wait(cell_id, yield_time_ms=1000, terminate=False)` 返回 running、completed、failed 或 cancelled，以及尚未读取的输出。重复读取不会重复输出。terminate 停止脚本，不能撤销已发生的业务效果，也不自动终止它启动的独立命令进程。

成功回执采用原生内容数组：第一项为 JSON 状态，后续为选出的文字／图片。例如：

```json
[
  {"type":"input_text","text":"{\"cell_id\":\"cell_...\",\"status\":\"running\",\"generator_id\":\"gen_...\",\"nested_calls\":1}"},
  {"type":"input_text","text":"已输出的部分内容"}
]
```

脚本失败在状态中附简短 error；已派发但中断、超时或未等待的调用可能附 outcome_unknown=true。普通参数错误仍使用框架原有错误回执。completed 只说明脚本结束，不代表所有业务目标已验证完成。

外层回执沿用统一工具输出预算，完整预算前结果留在 trace／输出归档。发生截断时，cell_id、status 等关键字段额外保留在预算 JSON 的 code_execution 字段中，避免丢失后续 wait 所需的 ID。图片沿用原生图片输入，不作为 base64 文字截断。

## 脚本接口

```javascript
const results = await Promise.all([
  tools.read_file({path: "a.txt", start_line: 1, end_line: 20}),
  tools.read_file({path: "b.txt", start_line: 1, end_line: 20}),
]);
text(results.map(r => r.output));
store("selected", {count: results.length});
```

- `tools.<name>(args)` 返回 Promise，解析为 `{output: ...}`。output 保持原工具的文本或原生内容数组；需要解析 JSON 时使用 `JSON.parse(result.output)`。普通工具错误可以作为原有错误 output 返回，桥接错误会拒绝 Promise。
- 内置工具使用原名；namespace 成员通常为 `namespace__member`，例如 `tools.mcp__demo__send_message`。名称冲突会添加稳定后缀，准确名称可从 `ALL_TOOLS` 查询，不通过模糊拼接决定路由。
- `ALL_TOOLS` 是该 cell 启动时的名称、说明和 deferred 标志目录，不将全部目录自动加入模型上下文。
- `text(value)` 选择交给模型的文字或 JSON 数据，不向用户发送消息。基础内置通信渠道使用 `tools.send_user_message`；其他已连接渠道使用各自的通信工具。
- `image(result)` 选择原生图片，例如 `image(await tools.view_image({path:"chart.png"}))`。也可传图片 data URL；不能传文件路径或外部 URL 来绕过图片工具。
- `store(key,value)` 保存 JSON 临时数据；`load(key)` 返回副本，不存在时为 undefined。每个 cell 看到启动时的 store 快照及自己的写入；提交的更新可供后续 cell 使用。它不属于私有记忆或 WorldState，不自动进入上下文，重启后清空。

普通中间结果只返回脚本，不分别触发模型 Step；完整嵌套调用与结果记录为 `code_mode_nested_call/result`，不伪造模型原生调用。模型实际发出的 exec/wait 才各自拥有普通配对工具回执，并进入现有事件队列。必须 await 所需调用；脚本结束时仍未等待的调用被取消并将结果标为不确定，不自动重放。

## 与事件框架配合

```text
输入事件 → Step → 模型调用 exec → 短脚本完成 → tool_result 事件
                               → 超时让出 → running 回执事件
                                          → 后台 cell 完成
                                          → code_execution_completed 事件
                                          → 模型调用 wait → tool_result 事件
```

只有初次让出后仍在运行的 cell 注册完成发生器。短脚本不重复发送完成通知，长脚本也不会为每条中间输出制造通知。模型可以在期间处理用户插话或 wait_for_event。wait 已观察最终结果时，尚未发出的通知可省略。

事件发生器类型为 `code_execution`，通知附 cell_id、状态和是否有未读输出。取消发生器只取消通知，不停止脚本，与命令完成通知的语义一致；停止脚本使用 wait 的 terminate。

Session 暂停先暂停发生器，再取消并清理 cell；恢复只恢复通知注册，不恢复 isolate、临时 store 或执行中的调用。尚处 active 的旧完成源报告 generator_unavailable，禁止把旧脚本当成待重放任务。

## 状态与工具边界

私有记忆修改只能由模型直接调用 `write_memory` / `apply_memory_patch`；它们不在 JS 目录中。`compact_history`、工具组／自定义工具管理、嵌套 tool_search、递归 exec/wait 以及内部 wait_for_event 也不开放，仍用直接工具。这样不需要隐藏或延迟发布记忆版本证据。

MCP、自定义脚本、宿主 namespace 等普通工具共用现有路由。延迟工具先由模型直接 tool_search 加载；每次新的嵌套派发都检查当前可见定义和路由，不用旧 cell 的绑定绕过 compact 或版本检查。移除服务可能中断旧连接上的在途调用，不自动将它改投新连接或重试。

JavaScript 在独立进程的 V8 isolate 内运行，没有 Node、Python 对象、直接文件或网络入口。仍可通过获准的宿主工具做相应操作。这里不是给已有 shell 增加 OS 沙箱：命令仍具原有宿主权限，禁止通过它绕写私有记忆，既有记忆不变量检查继续生效。

## 资源与范围

最多 8 个活跃 cell，最多保留 128 个结果单元；读取完的旧结果可在达到数量上限后回收。单 cell 最多 128 次嵌套调用、100 万字符输出、4 张图片；V8 堆限制 128 MiB，worker 发起的单次 JS 求值限时约 0.5 秒，异步执行仍受整个 cell 的墙钟限制。墙钟时限为 100–300000 ms，yield 等待为 0–30000 ms。临时 store 总体限制 4 MiB。消耗后的输出不再额外保留在 cell，已结束 cell 会释放 Step 上下文和 handler 引用。

本轮没有 notify、yield_control、audio、工具搜索的 JS 专用模式、工作笔记或脚本跨重启恢复。

验证见 `tests/test_code_mode.py`：实际 V8、并行和名称冲突、增量输出、事件唤醒、取消、CPU 限时、图片、预算、私有记忆入口、真实 MCP 定义的 compact 边界、正常暂停恢复等。真实模型验收产物不随项目分发。

参考上游 Codex `c419fd0` 的 code-mode / code-mode-runtime / code-mode-host 分层，及
[PyMiniRacer API](https://bpcreech.com/PyMiniRacer/api/)。


默认命名空间对齐：顶层工具和显式 `functions` 成员内部属于同一默认组。脚本中均使用 `tools.<name>`；其他组继续使用 `tools.<namespace>__<name>`。`functions.exec/wait` 的直接工具调用可省略 namespace，但它们仍不进入脚本自身的可调用目录。记忆写入和上下文管理的禁止规则按归一后的默认身份执行。

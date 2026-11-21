# 完整归档、有效历史与查询

历史分层、稳定 ID 和查询已接入主动历史替换。完整 compact 机制见 [compact.md](compact.md)。
本文的 archive/effective 游标工具参数是旧版 Python 兼容接口，已不再向模型公开；
当前编号、稳定路径、范围读取和搜索见 [history-navigation.md](history-navigation.md)。

## 对照 Codex 的边界

参考上游 Codex，提交 `c419fd01a70dd1d05356da443ac44328bda2e6f4`：

- `codex-rs/core/src/context_manager/history.rs` 的 `ContextManager` 管理模型窗口，
  `record_annotated_items` 明确区分 live history 的截断与 rollout 的完整原文。
- `codex-rs/core/src/session/mod.rs` 的记录入口负责持久化 rollout，
  模型请求使用捕获的上下文快照。

本项目沿用 Session 统一记录、ContextManager 提供模型历史、rollout 持久化的职责划分。
独立归档索引、以下 ID 格式与 `query_history` 是我们为主动 compact 增加的设计，
不声称是 Codex 同名工具或日志格式。

## 三种记录

1. `rollout.jsonl` 是持久化的完整运行日志，包含事件入队、历史条目、WorldState、
   检查点和 Session 边界。尚未消费的事件仍在队列和检查点中，不提前加入历史。
2. `HistoryArchive` 是当前 Thread 已进入历史的完整条目归档。输入、初始上下文、
   模型输出、已消费的工具回执及恢复通知均保留原始内容，不提供修改或删除接口。
3. `ContextManager` 用独立的 ID 列表表达有效历史。`snapshot()` 只解析这份引用列表，
   是 StepContext 构建模型请求的入口。范围压缩用摘要 ID 替换所选引用，归档原文不变。

归档存一份条目内容，有效视图存引用；读取返回副本，外部修改不会污染归档。
归档目前随 Thread 恢复加载到内存，尚无按需从磁盘建立索引的实现。
完整原文归档保留原始到达顺序；Responses 适配层的工具回执排序独立执行。

## 稳定 ID 与持久化

ID 为 Thread 内单调递增的 `h000000000001`、`h000000000002` 等。
它标识归档条目，不代表有效视图中的位置，也不是 `call_id` 或事件 `event_id`。
新条目从归档序列尾部分配；完整身份由 Thread ID 和条目 ID 共同决定。

Session 将 ID 写在 `response_item` 日志行的顶层 `history_id` 字段，
原始 `payload` 保持不变。恢复时验证显式 ID 与归档顺序一致；不一致则拒绝恢复。
旧日志缺少 ID 时按原始条目顺序重建同样的 ID，不改写旧日志。
继续运行的新记录带显式 ID，混合日志再次恢复仍保持一致。

`revision` 是已归档条目的数量，每追加一条递增一次，包括摘要及记忆检查点。
独立的 `generation` 每次替换递增，用于提交校验和有效视图分页一致性。
摘要和可选记忆检查点由一条 `history_compacted` 日志记录重建，不另写重复 response_item。

ID、revision 不作为 Responses 条目字段，也不在每步上下文前部插入动态编号。
查询回执会把需要的 ID 告知模型；trace 的 `history_recorded` 记录本批 ID 与 revision。

## 旧分页接口（仅兼容）

`query_history(view, cursor, start_id, limit, max_chars)` 已注册给 Session 的 Agent。

| 参数 | 含义 |
| --- | --- |
| `view` | `archive` 完整历史，或 `effective` 模型使用的历史 |
| `cursor` | 首次查询用 `null`，继续查询传上页的 `next_cursor` |
| `start_id` | 首次查询可指定起始 ID，包含该条；否则 `null`，从最早一条开始 |
| `limit` | 本页最多 1–50 个条目片段 |
| `max_chars` | 整个返回 JSON 的字符上限，1024–24000，包含转义、元数据和游标 |

示例：

```json
{"view":"archive","cursor":null,"start_id":null,"limit":10,"max_chars":12000}
```

返回 `view`、`snapshot_revision`、`effective_generation`、`entries` 和 `next_cursor`。
每个 entry 包含 `id`、`item_type`、`offset`、`total_chars`、`item_json`、`complete`。
`item_json` 是归档条目的 JSON 文本或其片段；按同一 ID 的 offset 顺序拼接，
在 `complete=true` 时得到完整 JSON。offset 和字符预算都按 Python Unicode 字符计算，
不是字节或模型 Token。

大条目可以跨页完整读取，不会因一次输出上限永久丢弃剩余内容。
JSON 被分段时，单个片段不保证可以独立解析；必须先拼接完整条目。
下一页传回 `next_cursor` 并将 `start_id` 设为 `null`，可调整每页预算。
`next_cursor=null` 表示当前查询快照读完。

首次查询固定归档上界。之后的查询调用、回执、新用户输入及事件都不会延长这次查询，
避免 Agent 查询历史时不断追逐自己产生的新记录。新查询可以看到新条目。
游标限定在同一 Thread 和同一视图；正常关闭恢复后仍可使用，不能跨 Thread 或视图复用。
effective 游标在发生新的压缩后失效，需要重新查询；archive 游标不因压缩失效。
冻结上界按 ID 对应的归档序列判断，不把摘要在有效历史中的位置当作归档顺序。
游标只是分页位置，不提供额外访问权限。

查询读取的是历史证据。旧用户消息、工具输出和事件不会因被查询而成为新的指令或授权。
查询不会执行旧工具、消费队列、取消后台进程或改变事件发生器注册。

`query_compaction(summary_id, offset, limit)` 分页查询摘要的直接来源 ID，并返回
直接替换条数、递归覆盖原始条数。来源中的摘要可以继续逐层展开，再用 archive 查询读取原文。

## 验证

`python3 -m unittest discover -s tests -v` 包含历史分层、不可变读取、冻结分页、
超长混合字符内容重组、非法游标、旧日志、ID 恢复及真实调度器工具调用的确定性测试。
`python3 scripts/history_query_agent.py` 使用 `.env` 配置的真实模型，
验收双视图查询和同一 Thread 恢复后的游标续读。
真实测试记录完整请求、回复及工具过程；原始运行产物不随项目分发。

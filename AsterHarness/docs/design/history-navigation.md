# 按需历史目录、摘要来源与远期召回

当前模型接口以本文为准；`history-query.md` 的 archive/effective 游标 API 保留为 Python
兼容入口，不再作为模型公开 schema。归档格式没有变化，旧 Thread 和旧 compact 记录仍可恢复。

## 两种身份

每个归档 item 有永久的 Thread-local `h000000000001` ID。StepContext 仍捕获不可变
HistoryView （有效 ID 序列、替换 generation、归档 revision)，但不再渲染 `[1]`标记、
不额外伪造 assistant 消息，不改写工具结果或 reasoning，也不每步追加目录。
Prompt.history_labels 仅保留为兼容槽，运行时不再生成、传输层不再使用。

需要编号时调用 query_history_directory；工具结果给出独立快照及 view_id。数字属于这张
目录，不是 API input 的数组下标。兼容层仍可能对工具回执排序，固定指导/独立记忆也不在目录。
普通追加不改动快照；成功 compact 后回执提示重新查询。旧目录作为历史记录仍可读取，
不会因压缩自动改写，清单细节无需在后续摘要中逐条保留。

view_id 绑定 Thread、generation 和归档 revision。依据不可变摘要来源反向重建历史快照，
无需额外维护永久目录注册表；正常 resume 后仍可按原 view_id 查询/分页。
此机制不是授权令牌，也不增加跨 Thread 查询权限。没有新增任意崩溃恢复保证。

## 寻址

工具 target 接受：

| 示例 | 含义 |
| --- | --- |
| `3` | 指定 view_id 的目录中第 3 个逻辑 item |
| `3-5` | 第 3 条摘要的第 5 个直接来源 |
| `3-5-2` | 再进入摘要的第二个直接来源 |
| `h000000000030` | 归档中该永久身份，可已不在有效历史中 |
| `h000000000030-5-2` | 稳定 ID 为根的来源路径 |

每一层是 1-based 直接来源位置，中间节点必须为摘要。数字根必须传 view_id；稳定根传 null。
结果和续读位置返回稳定 ID。来源关系不可变，继续分页不依赖当前顶层编号。
稳定 ID 不增加跨 Thread 权限，不能在其他 Thread 按相同 h-ID 找同一条记录。

## 工具

### query_history_directory(view_id, offset, limit, max_chars)

首次 view_id=null，返回 view_id、item_count、entries、next_offset。每个 entry 有 position、
稳定 id、type、短 preview，可带 role、name、call_id；摘要还有 is_compaction=true。
不返回全文，不把密文推理当正文。offset 从 0 开始，limit=1-50，max_chars=1024-24000，
预算限制整个 JSON。续页保持原 view_id，使用 next_offset；目录不会追逐新追加历史。
快照冻结于发起查询的请求，排除这次目录调用及结果；下一张新目录可包含它们。

### compact_history(view_id, start, end, summary)

start/end 是目录位置整数或可见条目的稳定 ID，包含两端。数字须先查询目录并传 view_id;
稳定 ID 可直接使用并传 view_id=null。文件/记忆准备不必进入特殊模式，可在查询前后执行。
Harness 按快照解析稳定 ID，检查范围仍是相同的连续可见记录，再调用既有单记录提交；唯一工具调用、
完整调用/回执配对和当前响应批次保护保持不变。独立记忆基线不属于编号视图。
记忆版本归并规则见 memory-projection.md；后段快照插入摘要之后，不移到尾部。
成功结果含 status=completed、summary_id、compacted_at、directory_view_id、effective_generation、
replaced_count、leaf_count、memory_checkpoint_added、memory_baseline_advanced、notice。
时间由 runtime 生成。notice 提示此前目录过期，下次 compact 前重新查询；不再额外发通知 item。
不实现一律拒绝旧目录，但选区已变更时拒绝执行，不能将旧位置解释成新位置。
不能修改已封存在旧摘要内部的选区；先只替换顶层有效历史。

### query_compaction(target, view_id, offset, limit)

返回目标 id、直接来源 item_count、原始记录 original_item_count、最大嵌套深度 max_depth,
以及 compact_items 中的子摘要 position、id、original_item_count。不列全部普通子项 ID。
offset 是从 0 开始扫描的直接来源位置，limit 是最多返回的子摘要数(1-50)，
next_compact_offset 是下一次扫描起点；使用返回的稳定 id 继续。此计数/深度是结构数据，不是重要性。

### query_history(target, view_id, offset, max_chars)

读取一个 item 的可读内容。返回稳定 id、items （单个片段）和 next。
条目片段有 position、id、is_compaction、offset、total_chars、content、complete。
不再将整个原 item 编码成 item_json 后切割；工具调用显示名称和 arguments，工具回执显示原 output，
message 显示正文。reasoning 仅呈现可返回的 summary，不把密文伪装成可读记忆。
外层仍是 function_call_output.output 字符串，内部 JSON 正常承载 content 字符串。

### read_compaction_range(target, view_id, mode, start, end, offset, max_chars)

target 必须是摘要。children 按直接来源编号读取，子摘要只返回其摘要正文；originals 跳过
中间摘要，按来源顺序读取原始叶记录。两个模式的 start/end 是不同的序列，不可混用。
返回 parent_id、mode、items、next。next 为稳定 target、position、offset；继续时改 start 为
next.position，offset 为 next.offset，保留原 end/mode。允许大 item 跨页，不静默丢弃尾部。
max_chars 为整个 JSON 上限（1024-24000)，不是正文/字节/token 上限。
单条读取用相同分页结构，但 position 固定为 1，续读只需稳定 target 和 offset。

### search_history(query, target, view_id, cursor, limit, max_chars)

字面量、大小写不敏感搜索，不是语义搜索，不接收正则。query 1-200字符。
target=archive 搜索完整归档 （含摘要）；摘要 ID/路径则只搜索其覆盖的原始叶记录。
结果包括稳定 target、snapshot_revision、matches、next_cursor；每个命中只返回 id、
is_compaction、match_offset 和短 snippet；用 query_history 按稳定 ID 读取上下文核实。

首次调用冻结在产生调用的请求归档上界，不让当前 search 调用自身成为命中。
游标绑定 Thread、查询文本、解析后的稳定根、上界和下次扫描位置，可跨正常 resume。
提供 cursor 时沿已冻结的根继续，不重新使用可能漂移的数字根；query 不能改变。
每次在整个冻结范围内搜索，不再按固定条数截断。找到要求数量的匹配（最多 20 个）、
达到输出字符预算或约 1 秒扫描软时间预算时返回。时间预算在记录之间检查，保证每次至少
处理一条；单条大记录处理和历史准备不能中途抢占，不是工具总耗时的硬上限。
回执包含 `complete`、`stop_reason`（`complete` / `match_limit` / `output_budget` / `time_budget`）、
本页已消费记录数 `scanned_count` 和冻结范围记录总数 `total_count`。完整搜索且没有命中时，
返回空 matches、complete=true、next_cursor=null。未搜完的空页不是不存在的证据；
继续传 next_cursor，直到 complete=true。旧游标仍然有效，不改变快照和 Thread 隔离规则。
历史中的旧查询和回执仍属于归档，可能产生重复命中；搜索结果不是事实已验证的证明。

## 高度不平衡的树

保留真实摘要溯源关系，不为平衡而改写旧摘要。深度在提交/重放时由子节点元数据计算。
originals 导航采用显式栈和叶计数，跳过不相关子树，无 Python 递归、无需把整树一次展开成正文。
范围读取和摘要内搜索按需迭代叶记录；已测试 1200 层链和恢复后的嵌套来源。

这还不是平衡索引：极深单链仍有深度相关的导航成本，全局关键词搜索仍为线性扫描，
归档仍整体载入内存。以后可加独立平衡序列索引、倒排索引/SQLite FTS 和混合检索，
不改变既有来源关系。当前没有向量库、时间衰减删除、自动遗忘或自动主题重组。

## 近期与远期

近期工作直接在有效历史中，容易看到；旧工作通过摘要保留概要，原文仍可搜索和定点读取。
这是可达性差异，不是按时间删除原文。摘要应保留关键名词、判断、限制和来源线索，
使未来查询有抓手；关键词搜不到不能证明事情没有发生。任意损坏/丢失日志仍不在恢复保证内。

## 本次验证

本地回归共 331 项通过，其中历史导航 28 项。覆盖原生请求无编号污染、目录整体输出预算、
固定快照分页、Thread 隔离、嵌套压缩及检查点的旧视图重建、查询后更新记忆再压缩、
完成回执入队后暂停/resume、旧目录清单的后续压缩，以及选区内部变化时拒绝误压缩。
旧目录未被一律禁用；未变选区仍可正确解析，提示词及成功回执要求下次压缩前重新查询。
这次是确定性流程/回归测试，未运行真实模型长任务，不能当作模型遵循新提示词的验收。

## 验证

```bash
python -m unittest discover -s tests -p test_history_navigation.py -v
python -m unittest discover -s tests -q
python scripts/navigation_check.py
python scripts/compact_check.py
```

navigation_check 建立合成旧事实，实测命令、直接按编号压缩、不保留暗号确切值，再正常恢复。
测试先断言暗号不在有效 input 中，随后 Agent 搜索旧关键词、按稳定 ID 读回证据，再批量读取
children/originals，最后向用户报告准确暗号。全部请求和错误保留，无隐藏重试。

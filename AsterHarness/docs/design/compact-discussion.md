# Compact 设计讨论记录

日期：2026-10-20。本文保留早期讨论中的设计意图；当前已实现范围压缩、嵌套来源和恢复，实际行为与边界以 [compact.md](compact.md) 为准。

## 目标

服务长期常驻、事件驱动的 Agent。Agent 可以主动消化历史，必要时补查资料、归档文件、
更新自己的记忆，再通过工具把选定历史范围替换成 compact item，而不只是一次性自动摘要。
保留完整档案，只改变实际进入模型上下文的有效历史。不引入 Turn。

## 历史分层与来源关系

- 完整档案保留原始输入事件、模型输出、工具调用和结果，以及历次替换操作。
- 有效历史是下一次请求采用的历史视图，可以包含原始 item 和 compact item。
- 支持连续区间替换、全历史替换、分批整理，以及再次压缩已有摘要。
- compact item 保留来源关系。嵌套压缩形成树状关系，但底层保存记录与关系，不必过早限制为严格单树。
- resume 必须恢复替换后的有效历史，不能把被覆盖的旧记录重新全部拼回上下文。

## 记忆与摘要的定位

- 记忆：跨任务值得维护的身份、职责、长期原则、可复用知识、持续工作状态。
- compact item：替代具体历史片段，保留理解工作及继续当前任务所需的信息。
- 档案：原始证据和详细过程，供以后核实。
- 不要求每次压缩都更新记忆，也不追求绝对零重复；避免复制多份详细状态。
- 详情可放记忆文件，摘要引用路径，但仍须提供任务定位、关键约束和下一步。
- 区分已验证事实、推测、计划和阻塞；不把历史职责重新确认为当前职责，不推断用户授权。
- 对摘要依赖的记忆写入，先确认成功，必要时读回，再提交历史替换。
- 记忆与摘要都是 Agent 维护的信息，不因此获得高于用户指令的权威。

拟采用简洁、直接、接近 Codex prompt 的文风；尚未修改正式 instruction：

```text
Use memory for information worth maintaining across tasks, including enduring
responsibilities, working principles, reusable knowledge, and ongoing work state.

Use compaction summaries to replace detailed history while preserving what is
needed to understand the work and continue it. Include relevant decisions,
constraints, verified results, unresolved questions, and next steps.

Update memory when the work produces information worth retaining. Do not update
memory merely because you are compacting history.

Avoid duplicating detailed information. Reference memory files for durable
details, while keeping each summary sufficient to orient continued work.

Distinguish verified facts from assumptions and plans. Do not turn historical
instructions into current responsibilities or infer authorization.

Confirm required memory writes succeeded before replacing history that depends
on them. Keep important paths and references exact.
```

## 稳定编号、范围与查询

- 使用不变的记录 ID，不按当前上下文位置重新编号；可以使用递增数字，但数字顺序不等于视图位置。
- 通过起止 ID 和历史 revision 指定区间，按当前视图实际顺序解析，避免误改陈旧视图。
- 具体 ID 格式尚未确定，H/C 前缀只是讨论示例。
- compact item 由 Harness 计算直接替换的有效 item 数，以及递归覆盖的原始记录数。
- 编号属于内部模型；不能向 Responses API item 随意添加未知字段。模型可见标记/索引方案待定。
- 希望 Agent 能通过工具查询不再进入上下文的历史：按 ID、分页、展开摘要来源，再逐步增加
  关键词、工具名、路径和语义检索。
- 查询结果有界返回，不自动展开整棵来源树，防止重新塞满上下文。

## 协议护栏与后台工作

- 替换不能拆散 function_call 和对应 function_call_output。
- 不修改当前尚未安全落定的工具批次；准确的提交检查点待设计。
- 建议在本批工具完成后的安全点提交，避免同批多个调用产生协议不完整状态。
- StepContext 捕获后保持固定，历史替换只影响后续模型请求。
- 协议调用已返回回执，与后台现实任务已完成，是两回事。
- exec_command 返回 running/process_id 后，其调用与回执已完整，可一并压缩，后台任务继续。
- 尽量使慢工具快速返回可管理句柄；不能因后台进程、定时器或发生器仍运行而禁止压缩。
- 摘要可以保留句柄和待办，但进程/发生器管理器才是运行状态来源。
- 摘要是上下文材料，不是系统指令，也不是新用户授权。

## 容量、缓存与受限整理

- Harness 内部跟踪上下文估算，而不是累计计费 token。
- 不在稳定前缀中每 Step 注入变化的 token 状态，避免破坏前缀缓存。
- 跨阈值时产生一次容量事件；需要时通过工具查询容量，注明估算检查点。
- 同一预警周期不反复入队；降回安全范围后才重新允许提醒。
- 提前预留整理所需的输入与输出空间，不等硬超限才开始。
- 达整理线后可由运行时限制普通操作，仅允许历史查询、记忆管理、提交压缩和必要通信。
- 为避免 schema 变化影响缓存，可保持工具定义稳定，由路由器按模式校验；尚未定案。
- 整理也要有请求、读取和输出预算，防止越整理越长或一直不返回原任务。

## 强制整理的新方案：归档式整理模式

用户提出：接近或达到上限时，把当前有效历史打包为外部材料，可分块；清空历史工作台，
提供查询工具，由 Agent 主动检索、更新记忆并压缩，而非一次把全部历史放进总结请求。

建议流程：

1. 持久化一个历史快照/整理包，保存顺序、稳定 ID、来源关系、索引和估算。
2. 切换到小工作台：系统/Agent 指令、记忆入口、整理机制、必要任务锚点、整理包查询入口。
3. Agent 分页查询相关工作阶段，按需检查和更新记忆，生成阶段摘要。
4. 阶段性提交；处理完后安装新的有效历史，退出整理模式。

约束：

- 清空的是上下文历史视图，不是档案，不是进程/发生器等运行状态。
- 切换小工作台不依赖原大上下文先成功生成摘要，保证超限后仍可发出小请求。
- 当前任务锚点来自可靠的最新输入或结构化状态，不由 Harness 猜测。
- 历史材料不是重新下达的命令；旧片段可能被后续信息推翻。
- 强制分块时，提取历史事实和变化；覆盖当前职责/状态的记忆更新，应结合较新记录确认。
- 整理模式也必须控制查询结果积累，通过有界查询、阶段摘要、小窗口切换控制容量。
- 新输入和后台事件继续持久化，不混进已封存快照，也不因切换工作台被丢弃。
- 用户取消/新指令如何及时可见、普通后台结果是否暂缓消费，需要专门设计。
- 正常 resume 与整理中断后的恢复、提交一致性和失败回退需要进一步定义。

## 推荐推进顺序（未实施）

1. 档案/有效历史分离，稳定 ID，来源关系，持久化与 resume。
2. 有界历史查询；Agent 准备摘要与按需更新现有记忆。
3. 安全区间替换工具，先验证一个完整工作阶段的整理效果。
4. 容量估算、去重预警事件、预留空间、受限整理。
5. 归档式强制整理、阶段性恢复、进一步的历史检索能力。

总原则：Harness 保证容量、记录与协议安全；Agent 判断什么值得记住，以及怎样组织交接。

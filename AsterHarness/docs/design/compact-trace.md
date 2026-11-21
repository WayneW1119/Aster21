# 简洁、完整的运行记录

原始 JSONL 不变，不影响实际模型输入、Thread 历史或调度。

```bash
python -m aster.compact_trace session-1.jsonl session-2.jsonl --output FULL_RECORD.md
```

每次实际 API 请求保留完整动态 `input`、模型名和所有其他参数。
`instructions`、`tools` 用 SHA-256 内容引用去重，第一次完整展示；内容变化则新增完整定义。
部分网关会在响应中回显这两个字段，响应也共用同一份字典。
将 `fixed_refs` 指定的值按字段名放回 `body`，就能逐字段还原原始请求/响应。
所有原生输出、推理返回字段、token 用量、工具回执均保留，不猜测未返回的推理。
折叠区只影响展示，不省略内容。

不再重复展示内部 `model_request/model_response` 和历史全量快照，避免同一输入输出展示多次。
这些内部记录仍可在原始 JSONL 查看；发生器、入队、Step、压缩操作和工具执行结果单独保留。
最后一个不唤醒的发送回执也会展示，不依赖它是否出现在后续请求中。
不记录鉴权请求头。不能把它当作完整网络抓包。

## 验证

离线记录格式回归：

```bash
python -m unittest discover -s tests -p test_compact_trace.py -v
python -m unittest discover -s tests -p test_render_trace.py -v
```

需要真实模型时，可显式运行 `scripts/compact_check.py` 或
`scripts/performance_agent.py`，在独立工作区生成新的 trace，再使用上述命令渲染。
原始记录及生成的 Markdown 属于本地运行数据，不随源码分发。

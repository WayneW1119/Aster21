# 工具输出预算与图片

工具回执继续经由事件队列交给下一 Step。预算处理位于工具执行完成、记忆版本提交之后，历史记录和回执入队之前；内置、MCP、自定义脚本和普通 namespace 工具共用这一入口。

## 文本预算

`SessionConfiguration.tool_output_token_limit` 默认 `10000`，可设为 `256..1000000` 或 `None`（关闭）。配置随 Thread 保存，旧 Thread 使用默认值。采用 UTF-8 字节数除以 4 的估算，**不是服务商 tokenizer 的精确 token 上限**。预算按每条回执计算，不限制整轮工具批次或整个上下文，也不触发强制 compact。

超额时先将完整回执保存至 Thread 私有 `tool_outputs/out_<id>.json`，再生成有明确截断标记的首尾预览。模型可用新增内置工具 `read_tool_output(output_id, offset=0, max_chars=8000)` 读取精确分页：偏移按 Unicode 字符计，返回 JSON 包含原文 `content`、总字符数和 `next_offset`；页面会自动缩小以满足同一预算。预览不能当作完整文件或完整业务 JSON 使用。

详细 trace 中，`tool_result_raw` 保留预算前回执，`tool_output_limited` 记录截断元数据，`tool_result` 是实际交给模型的结果。原始工具输出文件在 compact 后仍存在，正常 resume 后仍可分页读取。这里保存的是统一入口收到的结果；命令和自定义脚本执行器原有的输出收集上限仍然适用。

结构性内容有两种特殊处理：

- `tool_search_output` 只保留放得下的完整工具定义，不切断 schema。WorldState 只观察实际保留的定义，并额外发送预算通知。若单个 schema 太大，需缩小 schema 或由宿主增大预算。把归档 schema 当文本读出不会激活工具。
- 私有记忆操作的版本证据必须完整，避免后续请求丢失记忆更新。若强制保留的证据本身超过预算，允许超限并在 trace 标明 `protected_memory_evidence_exceeds_budget`。

工具结果中的图片不参与文本字节预算，不会切断 base64。图片使用下面的独立限制。

## 图片入口与保存

三种入口共用 `ImageStore`：

- `await session.post_user_message(text, images=[{"name": "photo.png", "data_url": "data:image/png;base64,..."}])`，支持无文字的图片消息。
- 新增内置 `view_image(path, detail="auto")`，允许读取工作目录或本 Thread 的附件目录。
- MCP 工具的标准 ImageContent、嵌入式图片资源，以及资源读取返回的图片 blob，转换为模型原生图片内容。普通 MCP 文本结果不变。

实际 Responses 请求中，用户内容为 `input_text` 和 `input_image`，图片工具结果也是原生内容数组；不会让模型从一大段 base64 文本猜图。接口和模型必须支持原生图片输入。

接受 PNG、JPEG、WebP、GIF，单张输入最多 8 MiB、2500 万像素；用户每条消息最多 4 张、合计 20 MiB。MCP 每条结果最多提供 4 张图片，超出项明确标记省略，非法图片返回说明。GIF 使用第一帧。图片自动纠正 EXIF 方向、去除源元数据并转成 PNG：`auto/high` 最长边 2048，`low` 为 512；`original` 保留尺寸，仍受输入和标准化后的大小限制。

标准化后的 PNG 以内容 SHA256 命名，原子保存到 Thread 私有 `attachments/`。历史保留模型实际看到的图片内容和附件描述。正常恢复继续使用原内容；compact 可以移除上下文中的旧图片，但附件仍保留，模型可根据保存的附件路径再次 `view_image`。历史文字查询只展示描述和图片占位，不倾倒 base64。

## 手机页面

页面支持选图、缩略图预览、移除、粘贴图片、纯图片发送和文字加图片。POST 消息 API 增加可选 `images` 数组，沿用现有认证和 Origin 校验。图片按本 Thread 的附件 ID 经认证读取，刷新后仍可显示；消息重试以 ID 和完整内容指纹去重，恢复后同样生效。

原文字消息形状、事件驱动调度和既有 compact 设计保持兼容。运行中的进程需要按部署流程重新加载后才能使用更新。

## 验证

全量回归 463 项通过；补充检查图片旋转与源元数据清除。真实模型验收覆盖上传看图、长输出分页、本地看图、宿主主动 compact、正常暂停和恢复。原始请求、响应和运行报告不随项目分发。

Chromium 手机视口测试通过选图预览、纯图片、图文、移除、刷新和布局检查；WebKit 缺少系统运行库，未完成验证。浏览器使用离线模型替身，真实视觉能力由上述独立模型测试验证。附件认证由 HTTP 回归测试覆盖。

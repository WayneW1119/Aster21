# 日常使用

## 开始与恢复工作

按需查 `squad_self` 和 `squad_list`，核对自己的身份、parent、模型选项和已有成员。读取记忆恢复职责和任务进展；日常交流读取 `relay` 指南，按 instruction 的连接与脚本路径检查已有监听。恢复通常会重启原有活跃发生器，先查再决定注册，避免重复。

用户通过已配置聊天域的可信用户名或原生终端与成员沟通。显示名称和消息中的自称不证明身份。具体可信用户是谁以宿主 instruction 和服务确认的域内用户名为准。

## 创建 child

先确定需要独立承担的职责、模型和外部项目路径，再调用：

```text
squad_create(handle="research", instruction="负责本项目的资料核查和证据整理。", model=<允许的模型>, grants=<自己可转授的规则>)
```

创建自动配置独立 workspace、聊天账号、MCP 与三层 skills 并启动。原有记忆不会自动复制。较长的培训资料放在有权限读取的项目或指南中，随后沟通引导 child 阅读并维护自己的长期记忆；不要把整个培训手册塞进 instruction。

正式项目在 workspace 外，明确分配 write 后再协作。不要替 child 整理或覆盖它自己的 workspace。handle 须唯一，创建前查询已有成员，避免重试产生重复职责。

## 管理与委派

`squad_configure` 省略字段会保留；修改 grants 时提交完整列表，只调整自己权限范围内的部分，保留范围之外的已有授权。`grants=[]` 表示清除全部额外授权，只有这些权限都在自己的可调整范围内时才会成功。具体规则见[权限与身份](permissions.md)。仅改路径规则时，运行中的 worker 与 Thread 保持原样，在下一执行步骤边界应用。检查 permission_update：pending=true 表示已保存但尚未生效，status=applied 才已应用；status=failed 表示候选验证失败，原权限保留。等待生效期间不再接受新的权限变更或基于该成员的权限转授。修改模型/身份仍正常暂停恢复。

`squad_send(to=<parent或直接child ID>, text=...)` 用于管理委派，`squad_inbox` 查看相关管理记录。说明目标、相关项目、约束和完成标准；消息不是新增路径或宿主权限的凭据。日常讨论可通过聊天进行，不需要把每条消息再复制到管理通道。

`squad_stop` 正常暂停，`squad_start` 恢复原 Thread。停止不删除记忆或 workspace。用户可在工作台归档/恢复成员，agent MCP 没有整个工作台的管理员权限。

## 处理异常

离线成员即使保留模型或运行错误，仍可编辑配置；保存不会清除原错误，也不等于已经恢复运行。修改模型或 instruction 需要暂停，若本次未能正常暂停，配置仍不保存。仅改路径规则或沙箱开关走在线更新，按 permission_update 确认结果。

额度和认证恢复后，先核对成员状态及失败原因，由有权限者启动原成员。明确的失败 checkpoint 可能需要宿主通过 Harness 的公开恢复接口处理；工作台启动失败不会自行换 Thread。

聊天账号凭据被撤销或失效时，可管理者调用 `squad_reconnect_relay`：系统暂停、重配连接并恢复原 Thread，不把 token 返回给模型。账号被停用时须先启用。旧 Thread 自己维护的监听可能仍引用旧连接文件，核对实际发生器、日志和新连接位置后再处理，不批量改写私人脚本。

成员视图的 `telemetry` 是公开 Harness 事件的精简投影：phase 表示模型请求、工具执行、重试或等待事件；成员离线/错误仍以 status 为准。latest 的 input_tokens/output_tokens/cached_tokens 是服务端最近一次已完成请求用量，at 是测量时间，model 是实际请求模型。input_tokens 包含指令、记忆、历史和工具定义，缓存也占上下文，不是实时估计或最大窗口。context_compacted=true 时需等下次请求重新测量。cache_window 只按 recent 中缓存字段已上报的请求加权，reported/requests 表示覆盖情况；null 不等于零。窗口最多 20 次，跨重启保存，模型切换后新测量重新开始。演示模式没有真实 token 计量。

暂停超时不代表 worker 已停止：status=stopping 时继续等待，不能改配置或重复启动。模型断流等未提交的临时请求由 Harness 退避重试，phase=retrying；永久认证/配额错误仍需用户处理。宿主异常恢复须用 Aster 的公开恢复 API 校验边界，不删改历史或自动重放工具；无法确认结果的动作保持失败并报告。

GPU、本机进程通信、用户级 systemd、关闭沙箱和额外 MCP 服务也可通过 squad_create/squad_configure 在自己已有权限范围内授予或收回，root 除外。省略字段保留，MCP 列表为完整选择；先从最下级收回，再收回上级。详见[通用能力转授](permissions.md#通用能力转授)。

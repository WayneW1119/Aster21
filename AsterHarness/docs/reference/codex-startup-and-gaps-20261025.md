# Codex 启动上下文与 Aster 差距

核对日期：2026-10-25。工具清单是配置、模型元数据、客户端及账户能力共同决定的，不存在所有 Codex 实例通用的一份完整默认列表。

## 实验范围

- 验证版本：Codex CLI 0.160.0；实验使用 gpt-6.1-sol 模型元数据。
- 在独立 CODEX_HOME 中采用最小配置，将模型服务替换为 HTTP 记录器，观察首个请求；记录器只返回固定确认消息，不运行模型推理或工具。
- 参考源码来自 `https://github.com/openai/codex`，其版本与验证的 CLI 不保证完全相同。
- 该实验只说明此版本、模型元数据和隔离配置下的核心启动组合，不代表所有账户的云端工具或宿主插件集合。未成功取得请求的探针不作为证据。

## 1. 这份核心启动抓取有哪些工具

gpt-6.1-sol 元数据的 tool_mode 是 code_mode_only，use_responses_lite 为 true。
在这一具体请求中，工具以 input 内 additional_tools 提供，没有顶层 tools 字段。
这说明“模型可见工具定义”与“HTTP JSON 顶层 tools 数组”不能在所有 Codex 模式中画等号。
本 Harness 当前使用的普通 Responses / native search 方式不依赖此 additional_tools 扩展。

补充核对：additional_tools 本身是工具定义的输入项格式，但不等于它与 WorldState 无关。
源码中的 TopLevelToolsState 在启用增量工具模式时，由 WorldState 生成 additional_tools 定义更新，
并维护工具/组说明的增删变化。另一路是 client.rs 在请求前缀中提供完整定义。
这与本 Harness 的来源摘要和加载定义投影在目标上相似，但传输格式与具体状态范围不同。

直接可调用入口：

| Namespace | 工具 | 用途 |
|---|---|---|
| functions | exec | 在受控 JavaScript 执行器中组合底层工具调用 |
| functions | wait | 等待 exec 返回的异步执行单元 |
| functions | request_user_input | 结构化提问；定义注明仅在 Plan 模式可用 |
| functions | request_user_input_async | 提问后继续工作，回复随后进入会话 |
| clock | sleep | 可被新输入打断的等待 |
| collaboration | spawn_agent | 创建子 Agent |
| collaboration | followup_task | 向子 Agent 安排后续工作 |
| collaboration | send_message | 给已有 Agent 发消息 |
| collaboration | interrupt_agent | 中断 Agent |
| collaboration | list_agents | 查询 Agent 树 |
| collaboration | wait_agent | 等待 Agent 更新 |

注意：工具可见不代表无条件获准使用。该次上下文仍限制主动派生 Agent，request_user_input 也有模式限制。

通过 functions.exec 中 tools 对象调用的底层能力：

| 名称 | 用途 |
|---|---|
| apply_patch | 修改文件 |
| exec_command | 运行命令 |
| write_stdin | 与运行中的命令交互或获取输出 |
| view_image | 查看本地图片 |
| create_goal | 创建显式目标 |
| get_goal | 查询目标 |
| update_goal | 更新目标状态 |
| clock__curr_time | 读取时间 |

exec 是工具编排器，不等于 shell。JavaScript 本身没有直接文件系统、网络、console 等能力；
文件、网络、进程等行为通过宿主提供的工具执行。Promise 可组合独立调用，返回给模型的输出由 text/image 等接口控制。
这与我们执行一个脚本作为自定义工具不同：后者没有提供可在一次模型调用中组合多个宿主工具的编排环境。

其他模型或模式可能直接展示 exec_command / write_stdin / apply_patch 等，
也可能提供 update_plan、web search、不同版本的协作工具。不能把源码中的所有 handler 合并成默认启用名单。

## 2. MCP：客户端能力与宿主注入

隔离启动实验没有配置 MCP 来源，不能据此推断其他客户端或宿主也没有 MCP。
客户端配置、应用连接器及宿主插件都可能提供工具；工具目录可见，也不等于账户已获授权、服务可达或调用成功。

参考源码中的 codex_apps 是应用接入层的特殊服务标识。应用名字或工具的 mcp__ 前缀，
不一定代表每个应用都通过一个独立 MCP 服务器连接。应根据有效配置、工具来源说明和实际初始化结果判断。
MCP 通用资源工具只是客户端能力，不等于某个业务服务已连接。

## 3. Skill：发现目录不等于全文加载

隔离启动请求的 Available skills 中实际列出了：

| Skill | 用途 |
|---|---|
| imagegen | 图片生成与编辑工作流 |
| openai-docs | OpenAI / Codex 官方文档工作流 |
| skill-creator | 创建和更新 Skill |
| skill-installer | 安装 Skill |

启动时先提供名字、描述、位置及使用规则，需要时才读取相应 SKILL.md 全文及其引用资源。
Skill 可以只有说明，也可以附带脚本、模板，或声明对 MCP 的依赖；它本身不是一种工具协议。
技能目录中存在文件，不等于该技能在模型上下文中可见；还要看发现规则、有效配置与宿主提供的目录。
应用或插件附带的技能由具体宿主提供，不能将某次会话中的目录当成干净 CLI 的默认技能集合。

## 4. 启动上下文还包含什么

除工具定义外，通常还会组合：

1. 模型的基础行为指令和客户端的工作规则。
2. 工作目录、平台、日期、权限、沙箱与审批方式。
3. 仓库/目录的 AGENTS.md 指令（如果存在并适用）。
4. Skill 名称、描述、路径和加载规则。
5. MCP / Apps / Plugins 的发现与使用规则，以及来源摘要（按配置与模式）。
6. 当前模式、协作规则、宿主提供的动态能力。
7. 恢复会话时的历史或压缩内容；新会话则没有旧工作的历史。

WorldState 的参考源码还包含 environment、permissions、agents_md、model、model_catalog、
collaboration_mode、tools、tools_budget、context_window_guidance 等模块。
这些是可组合模块，并不表示所有模块都会无条件产生启动文本。

## 5. 调研时的 Harness 差距（2026-10-25）

以下为调研日期的历史状态；当前能力以项目文档和实现为准。

| 能力 | 调研时状态 | 仍值得补充的部分 |
|---|---|---|
| 事件、工具、历史基础链路 | 有 Step、事件队列、完整日志与正常恢复 | 持续扩展边界测试；保留自己的无 turn 设计 |
| 命令与文件补丁 | 有 exec_command、write_stdin、apply_patch | 更统一的权限与取消策略 |
| 私有记忆与上下文投影 | 有固定基线、更新证据、WorldState | 按需加入新的状态域 |
| MCP 与自定义脚本工具 | 有 stdio、资源、搜索、动态来源和本地工具管理 | MCP HTTP/OAuth、服务端目录变化通知、模型侧 MCP 配置入口 |
| Skill | 没有完整加载机制 | 元数据发现、按需读取、更新与恢复时保留必要证据 |
| 仓库指令 | 有 Thread instructions.md | 层级 AGENTS.md 发现、适用范围和来源变更 |
| 上下文预算 | 有 10 万 token 提醒和手动范围 compact | 请求前预算、统一输出预算、超限处理；强制压缩策略需另外讨论 |
| 工具输出 | 内置读/命令及自定义脚本有局部上限 | MCP 调用/资源等也需要统一预算，并保留完整原始日志 |
| Code Mode | 没有 | 一次模型调用中组合工具、控制中间输出、并行独立调用 |
| 多 Agent | 没有模型侧协作机制 | 子 Agent 生命周期、隔离上下文、消息、等待与中断 |
| 图片/多模态 | 没有完整用户输入到模型/工具的链路 | view_image、图片附件、图像工具返回 |
| 用户交互 | 有发送消息与异步插话 | 可选的结构化提问、待答问题管理 |
| 目标/计划 | 主要由模型通过记忆、历史和事件维护 | 可选的显式目标/计划状态，不引入对话 turn |
| 沙箱与审批 | 工具使用宿主权限，非沙箱 | 操作系统隔离、网络/文件策略、审批边界 |
| 传输恢复与中断 | 单次请求、正常暂停恢复 | 可取消请求、分类错误处理和安全重试策略；不能盲目重试有副作用工具 |

优先建议：先统一工具输出预算、加入 Skill 与 AGENTS.md；之后按需求补图片和结构化提问。
Code Mode、多 Agent、插件生态是更大的独立工程，不必为了对齐一次全部加入。
这些差距不要求把 Python 改写成 Rust。

## 官方说明与源码入口

- [配置层与优先级](https://learn.chatgpt.com/docs/config-file/config-basic)
- [Skills](https://learn.chatgpt.com/docs/build-skills)
- [MCP](https://learn.chatgpt.com/docs/extend/mcp)
- 上游源码：codex-rs/core/src/tools/spec_plan.rs、core/src/context/world_state/、features/src/lib.rs。

本文件只记录调查和对比，没有为 Harness 实施上述新增能力。

# 能力差异与后续方向

记录日期：2026-11-01。用于保留讨论结果，逐步补齐能力；不是本轮全部实施的承诺。Codex CLI、桌面端、插件和托管服务的能力不同，不能把它们当成所有 Codex 会话默认具备的一组工具。

## 已有基础

Harness 已有命令与文件工具、文件沙箱、图片输入和查看、MCP 接入与工具发现、skills、自定义脚本工具、code mode、上下文压缩、记忆、事件发生器和 Thread 恢复。Squad 提供空间、成员分支、授权、生命周期和工作台，Relay 提供独立聊天。

## 当前状态与后续方向

| 能力 | 当前状态 | 适合的实现位置 |
| --- | --- | --- |
| 网页搜索 | 已接入独立 Aster Web，统一工具显式选择 Exa / DeepSeek | 独立搜索 MCP 与使用 skill；[调研记录](research/web-search-2026-11-01.md) |
| 网页正文读取 | 已提供 web_fetch；错误明确返回，不自动切换供应商 | 与搜索服务组合，按需读取来源，不只依赖搜索摘要 |
| 浏览器交互 | 可写 Playwright 等脚本，没有标准浏览器工具与会话配置 | 独立浏览器 MCP、skill、浏览器会话和凭据管理 |
| 图片生成与编辑 | 已接入独立 Aster Images 生图/改图 MCP；按成员配置启用 | 独立 Codex 订阅 worker 与成员沙箱内 MCP；不绑定成员推理模型 |
| 桌面 GUI 操作 | 没有统一截图、点击和键盘控制通道 | 按操作系统接入的独立工具与授权 |
| 外部应用集成 | 已具备 MCP 基础，缺少现成应用连接与认证管理 | 按需接应用 MCP、凭据及 skill；不假定 Codex 插件可原样移植 |
| 通用 MCP 配置入口 | 已提供宿主服务目录与工作台成员勾选，凭据留在宿主 | 复用 Harness 标准 MCP；独立服务由部署者安装配置 |
| Git 工作流界面 | 可使用 Git 命令，没有工作树、差异审阅等专用工作台流程 | Squad 产品层，或独立开发工具 |

优先讨论搜索与网页正文读取，其次浏览器；其他能力按实际项目需求推进。现成 MCP 可以解决的功能，先复用，不为补齐清单而重写服务。供应商专属的托管能力和额度不因使用同一模型名称而自动继承。

搜索已通过独立 [Aster Web](https://github.com/WayneW1119/AsterWeb) 接入 Exa 与 DeepSeek。Harness 核心未添加供应商逻辑；Squad 只管理服务目录与成员选择。后续继续改善供应商覆盖与配额管理，详情和验证边界保存在上述调研记录中。

## 对照资料

- [Codex 网页搜索](https://learn.chatgpt.com/docs/web-search)
- [浏览器及产品可用范围](https://learn.chatgpt.com/docs/browser)
- [图片生成](https://learn.chatgpt.com/docs/image-generation)
- [Computer Use](https://learn.chatgpt.com/docs/computer-use)
- [Skills 与插件](https://learn.chatgpt.com/docs/build-skills)
- [Git 工作树](https://learn.chatgpt.com/docs/environments/git-worktrees)


## 图片能力接入

独立 [Aster Images](https://github.com/WayneW1119/AsterImages) 提供 image_generate / image_edit，委托一次性官方 Codex，使用 ChatGPT 登录，不转图片 API。部署者安装服务、配置宿主 MCP 目录并注册 images skill 后，可在工作台按成员启用；新建成员使用宿主目录默认值。接入格式与 skill 随独立项目提供。

Codex CLI 0.160.0 的 JSONL 未直接提供图片事件，适配器通过新 Thread ID 对应的原生生成目录读取并校验 PNG，不相信助手输出的任意路径。内部工具参数仍由 Codex 解释，不能保证逐字传递。失败不自动重试；超时可能已经消耗额度。

## Squad 套件的默认 Relay 连接（新成员）

套件条目使用稳定的内部 ID；安装到成员时，按 Relay skill 的约定，用域简称和成员绑定的 Relay 用户名生成工具命名空间 `relay_<域简称>_<用户名>`。默认账号监听的 `listen_name` 复用最终连接名。安装器遵守 Harness 的 48 字符和 ASCII 命名限制，遇到冲突时保留既有绑定，为新身份选择可区分的别名。首次绑定后保存该名，域显示名变化不触发自动改名。既有 Thread 的固定 `relay` 名称作为迁移兼容项处理，避免重复建立连接或监听。

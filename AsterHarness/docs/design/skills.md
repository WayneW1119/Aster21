# Skills

Skill 是按需读取的操作指南，可以附带脚本、模板和参考资料。它不是工具定义，不会自动注册工具、安装依赖、执行脚本、建立事件发生器或提升文件执行权限。

## 文件和发现

默认根目录为 `<workspace>/skills/`。配置 `SessionConfiguration.skill_directories` 或新 Thread 的重复参数 `--skills-dir PATH` 可添加根目录；相对路径以工作目录为准。目录配置持久化。也可通过 `skills.sources` 配置来源及是否展示路径；运行时用 `skills.load` 添加来源。

```text
skills/
  review-text/
    SKILL.md
    references/checklist.md
    scripts/check.py
```

`SKILL.md` 开头为 YAML frontmatter：

```markdown
---
name: review-text
description: Review a draft for clarity and actionable feedback.
---

先读取 references/checklist.md，再按用户要求检查草稿。
```

`name` 长度 1–64，小写字母或数字组成的段以单个连字符分隔；`description` 为非空字符串，最长 1024 字符，支持 YAML 折叠文本。元信息之后必须有正文。用安全 YAML 解析器，不执行其中的对象标签。支持 UTF-8/BOM 和 CRLF。

按配置根目录顺序发现，根目录内按路径排序；同名技能由先出现者生效，其余进入诊断。隐藏目录和子目录符号链接跳过，符号链接或非普通文件形式的 SKILL.md 不加载。不存在的根目录视为空；格式错误的单项不会阻断其他 skill。

发现过程最多检查 256 个 SKILL.md、4096 个目录、8 层子目录，单文件不超过 256 KiB。简介目录最多 64 KiB，诊断最多 32 条。超限会提示，未展示项不可当作已加载。第一版只对 SKILL.md 本身计算 SHA-256，附属文件变化不单独发更新。

## 模型可见内容

WorldState 顺序为 memory → skills → tools → 宿主其他部分，然后是有效历史。skills 中呈现名称（即 skill_id）、简介及 SHA-256，可选展示绝对说明文件路径；正文不自动注入。默认目录和旧 directories 配置保留路径，sources 与运行时加载默认不展示路径。空目录不增加 skills 前缀。三个专用工具始终可用。

首次创建 Thread 可将当前目录直接固定为基线。每次有事件触发 Step 时重新扫描；新增、删除、简介或正文变更追加差量 developer 消息，不改写先前前缀。更新记录内部保存完整状态用于精确还原，私有字段不会发给模型。扫描本身不唤醒空闲 agent，也不产生模型调用。

目录随附通用指导：根据用户任务匹配 skill，使用前读取 SKILL.md；说明不在当前上下文时重新读取；参考资料通过同一 skill_id 和包内相对路径读取，脚本按需通过普通工具执行；用户和运行时的规则仍优先。SHA-256 改变表示需要重新读取指南。

## 专用工具与来源配置

- `skills.read(skill_id, resource="SKILL.md")` 返回 skill_id、包内资源名、SHA-256 和文本。模型无需知道文件位置。参考资料使用 `resource="references/checklist.md"`；目前只支持 UTF-8 文本，单个资源最多 256 KiB，输出沿用统一预算及归档机制。
- `skills.load(source, expose_path=false)` 接受宿主目录或 SKILL.md 路径；相对路径以 workspace 为准。返回 accepted/request_id，在下一 Step 按请求顺序应用，并产生 `skill_change_applied` 或 `skill_change_failed` 事件。同一来源再次加载会重新启用其中被卸载的 skill。激活失败保留原状态。
- `skills.unload(skill_id)` 同样在下一 Step 生效，只移除目录项，不删除文件。卸载状态随 Thread 保存。

宿主在 `skill_sources` 注册的目录扫描和专用读取不受 Agent 文件系统沙箱限制。这个例外限于已注册包的 skill 内容；默认目录、`skill_directories`、agent 自行 `skills.load` 的来源及其资源均遵守文件权限。脚本仍由普通命令工具执行并受沙箱约束。包内资源读取拒绝越界路径、符号链接、特殊文件和非 UTF-8 内容；不提供写入能力。

`thread.toml` / 模板配置示例（相对路径以配置文件目录为准）：

```toml
[skills]
directories = ["/shared/visible-skills"]
sources = [
  { path = "/shared/private-location-skills", expose_path = false },
  { path = "/shared/other-skills", expose_path = true },
]
```

SDK 可使用 `SessionConfiguration(skill_sources=({"path": "/shared/skills", "expose_path": False},))`。SDK 直接创建时，相对路径以 workspace 为准。

默认目录、directories、sources、运行时来源按此顺序发现，同名 skill 保留最先出现者，并显示冲突诊断。运行时 load 参数和已存在的历史不会被抹去；不展示路径是让模型无需依赖路径，并非保证路径从未在其他上下文中出现。

工具回执沿原事件队列进入后续 Step。加载请求的内部路径保存在私有存档中，投影给模型时移除内部 `_skill_change` 字段。读取结果作为普通历史，不新增激活状态。sources 加载目录和卸载项保存在 skills section 的 v2 状态中；旧 v1 状态仍可恢复。

## Compact 和恢复

SkillsState 使用通用 WorldState 生命周期和 v4 section 状态存储。整体 compact 将当前目录提升为新基线；局部 compact 在被替换范围留下精确目录检查点，范围外的更新继续保留。完整说明读取作为普通历史被压缩，不自动保留或重放；需要继续使用时重新读取，进展放入私有记忆。

恢复先还原存档中的固定基线和当前状态，再将当前磁盘的变化追加到历史。不存在重启后悄悄用新文件覆盖旧前缀的行为。损坏的投影或不一致的历史证据会拒绝请求。压缩持久化失败时不应用候选状态。

无 skill 时不注入目录，仍提供 skills 工具以便运行时加载。首次发现后即使删除全部 skill，仍保留投影状态以正确解释删除和恢复。skills 是内置保留 section ID。

## 第一版边界

目前来源为本地目录或 SKILL.md；不提供远程商店、自动下载、依赖安装、插件集成或注册工具。模型可以在用户授权范围内通过现有工具创建指南，下一 Step 会发现；这些操作不会改变其权限。专用读取与文件沙箱独立，其他工具沿用原有权限。

测试覆盖发现规则、正文变更、差量前缀稳定性、真实文件工具读取、局部/整体 compact、保存失败、目录配置持久化、移除全部技能后的恢复、伪造投影拒绝和旧运行时兼容。真实模型验收产物不随项目分发。


## 来源与文件权限（0.1.1）

`SessionConfiguration.workspace_skills` 默认 true；托管环境可设 false，只注册 workspace 外的来源。配置文件对应 `[skills] workspace = false`。`skill_sources` 是宿主明确注册的读取能力，可带 `label` 展示来源范围；普通文件 deny 不阻止 `skills.read` 读取这些指定包内文本。

`skills.load` 是 agent 自行添加的来源，扫描和后续资源读取均遵守普通文件沙箱；恢复后的动态来源也重新按当前权限扫描，不能提升为宿主来源。`skill_directories` 和默认 workspace 来源同样遵守文件权限。同名 skill 保留先注册的来源，并返回冲突诊断。目录 source 字段在存在宿主 label 时标明来源；旧目录与历史消息仍可恢复。

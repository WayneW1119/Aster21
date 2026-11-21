# Codex MCP：完整工具定义与原文

本文固定在“服务已连接，工具采用延迟加载，尚未搜索／调用”的时刻。

这些 JSON 根据上游 Codex 的 Rust 默认构造器及 Responses API 序列化规则还原，**不是某次真实模型请求的抓包**。英文 description 和参数说明保留源码原文，不使用省略号。JSON 格式化和字段顺序不影响含义。中文翻译附在对应 JSON／英文文本后，不改动原始定义；Rust 附录中的重复工具文案可对照前面的翻译，其他说明和日志文字在代码后补译。

- 上游源码：`https://github.com/openai/codex`
- 提交：`c419fd01a70dd1d05356da443ac44328bda2e6f4`
- 场景：普通 Responses API、客户端工具搜索、普通函数模式；不展开 Code Mode 或 Responses Lite 增量声明。
- `tool_search` 默认 limit 为 8；参数的类型原样是 `number`，不是擅自改成 `integer`。
- 资源工具使用 `messages=None` 的默认定义。真实模型目录可以覆盖 description 和 parameters；因此不是所有模型都保证逐字相同。
- `strict: false` 原样保留；未设置的 `defer_loading` 不序列化，`output_schema` 也不发送。
- 本文只列出这四个相关定义，真实请求还可能有命令、文件等其他工具。
- 主 JSON 文件：[codex-mcp-tool-definitions.json](codex-mcp-tool-definitions.json)。其外层元数据和两个场景名是本文组织结构，不是 API 字段；场景内部的 `tools` 是对应数组。

## 1. 四个工具分别做什么

| 工具 | 操作对象 | 作用 |
|---|---|---|
| tool_search | 延迟工具的元数据 | 找到工具并返回可调用定义；不执行业务动作 |
| list_mcp_resources | 已连接服务公开的资源 | 列举具体 URI，支持按 server 选择和 cursor 翻页 |
| list_mcp_resource_templates | 服务公开的参数化资源模板 | 列举 URI 模板，例如 inventory://items/{sku} |
| read_mcp_resource | 一个服务上的具体资源 URI | 读取资源内容，可能是文本或二进制内容 |

资源表示服务提供的上下文数据，例如数据库结构、文档、配置或日志；URI 不要求是 HTTP，也不表示本机任意文件。服务可以只提供 tools、不提供 resources，列出资源可能得到空结果。模板列举不等于调用业务工具，也不等于自动读取内容。

例如一个库存服务可以公开 `inventory://schema` 资源供读取，并另外提供 `update_stock` 工具执行库存更新。这是两个独立的 MCP 能力。`tool_search` 的原始说明明确要求不要用资源列表来代替 MCP 工具发现。

## 2. 场景 A：来源列表写在 tool_search.description

本例直接采用 Codex 自带测试里的两个来源：Google Drive 和 docs。Google Drive 的英文说明也来自该测试；docs 没有说明，所以只显示名称。它们不是当前 Harness 的连接配置。

```json
{
  "tools": [
    {
      "type": "tool_search",
      "execution": "client",
      "description": "# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools for the next model call.\n\nYou have access to tools from the following sources:\n- Google Drive: Use Google Drive as the single entrypoint for Drive, Docs, Sheets, and Slides work.\n- docs\nSome of the tools may not have been provided to you upfront, and you should use this tool (`tool_search`) to search for the required tools. For MCP tool discovery, always use `tool_search` instead of `list_mcp_resources` or `list_mcp_resource_templates`.",
      "parameters": {
        "type": "object",
        "properties": {
          "limit": {
            "type": "number",
            "description": "Maximum number of tools to return. Defaults to 8."
          },
          "query": {
            "type": "string",
            "description": "Search query for deferred tools."
          }
        },
        "required": [
          "query"
        ],
        "additionalProperties": false
      }
    },
    {
      "type": "function",
      "name": "list_mcp_resources",
      "description": "Lists resources provided by MCP servers. Resources allow servers to share data that provides context to language models, such as files, database schemas, or application-specific information. Prefer resources over web search when possible.",
      "strict": false,
      "parameters": {
        "type": "object",
        "properties": {
          "cursor": {
            "type": "string",
            "description": "Opaque cursor from a previous list_mcp_resources call; omit for the first page."
          },
          "server": {
            "type": "string",
            "description": "MCP server name. Omit to list resources from every configured server."
          }
        },
        "additionalProperties": false
      }
    },
    {
      "type": "function",
      "name": "list_mcp_resource_templates",
      "description": "Lists resource templates provided by MCP servers. Parameterized resource templates allow servers to share data that takes parameters and provides context to language models, such as files, database schemas, or application-specific information. Prefer resource templates over web search when possible.",
      "strict": false,
      "parameters": {
        "type": "object",
        "properties": {
          "cursor": {
            "type": "string",
            "description": "Opaque cursor from a previous list_mcp_resource_templates call; omit for the first page."
          },
          "server": {
            "type": "string",
            "description": "MCP server name. Omit to list resource templates from every configured server."
          }
        },
        "additionalProperties": false
      }
    },
    {
      "type": "function",
      "name": "read_mcp_resource",
      "description": "Read a specific resource from an MCP server given the server name and resource URI.",
      "strict": false,
      "parameters": {
        "type": "object",
        "properties": {
          "server": {
            "type": "string",
            "description": "MCP server name exactly as configured. Must match the 'server' field returned by list_mcp_resources."
          },
          "uri": {
            "type": "string",
            "description": "Resource URI to read. Must be one of the URIs returned by list_mcp_resources."
          }
        },
        "required": [
          "server",
          "uri"
        ],
        "additionalProperties": false
      }
    }
  ]
}
```

对应文本的中文翻译（按字段位置列出，JSON 原文保持不变）：

| 字段 | 中文翻译 |
|---|---|
| `tool_search.description` | # 工具发现<br><br>使用 BM25 搜索延迟加载工具的元数据，并将匹配的工具提供给下一次模型调用。<br><br>你可以使用以下来源提供的工具：<br>- Google Drive：将 Google Drive 作为处理 Drive、Docs、Sheets 和 Slides 工作的统一入口。<br>- docs<br>某些工具可能没有预先提供给你，你应使用本工具（`tool_search`）搜索所需的工具。发现 MCP 工具时，始终使用 `tool_search`，而不是 `list_mcp_resources` 或 `list_mcp_resource_templates`。 |
| `tool_search.parameters.limit.description` | 最多返回的工具数量，默认值为 8。 |
| `tool_search.parameters.query.description` | 用于搜索延迟加载工具的查询词。 |
| `list_mcp_resources.description` | 列出 MCP 服务提供的资源。资源允许服务共享可为语言模型提供上下文的数据，例如文件、数据库结构或应用专用信息。条件允许时，优先使用资源，而不是网页搜索。 |
| `list_mcp_resources.parameters.cursor.description` | 上一次 list_mcp_resources 调用返回的不透明游标；请求第一页时省略。 |
| `list_mcp_resources.parameters.server.description` | MCP 服务名称。省略时，列出所有已配置服务的资源。 |
| `list_mcp_resource_templates.description` | 列出 MCP 服务提供的资源模板。参数化资源模板允许服务共享需要传入参数、并可为语言模型提供上下文的数据，例如文件、数据库结构或应用专用信息。条件允许时，优先使用资源模板，而不是网页搜索。 |
| `list_mcp_resource_templates.parameters.cursor.description` | 上一次 list_mcp_resource_templates 调用返回的不透明游标；请求第一页时省略。 |
| `list_mcp_resource_templates.parameters.server.description` | MCP 服务名称。省略时，列出所有已配置服务的资源模板。 |
| `read_mcp_resource.description` | 根据服务名称和资源 URI，从 MCP 服务读取指定资源。 |
| `read_mcp_resource.parameters.server.description` | MCP 服务名称，必须与配置中的名称完全一致，并且与 list_mcp_resources 返回的 server 字段一致。 |
| `read_mcp_resource.parameters.uri.description` | 要读取的资源 URI。必须是 list_mcp_resources 返回的 URI 之一。 |


## 3. 场景 B：来源列表交给 WorldState

启用 `DeferredToolWorldState` 时，搜索工具定义不再重复列出来源；其他通用搜索指导不变。完整相关数组如下：

```json
{
  "tools": [
    {
      "type": "tool_search",
      "execution": "client",
      "description": "# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools for the next model call.\n\nSome of the tools may not have been provided to you upfront, and you should use this tool (`tool_search`) to search for the required tools. For MCP tool discovery, always use `tool_search` instead of `list_mcp_resources` or `list_mcp_resource_templates`.",
      "parameters": {
        "type": "object",
        "properties": {
          "limit": {
            "type": "number",
            "description": "Maximum number of tools to return. Defaults to 8."
          },
          "query": {
            "type": "string",
            "description": "Search query for deferred tools."
          }
        },
        "required": [
          "query"
        ],
        "additionalProperties": false
      }
    },
    {
      "type": "function",
      "name": "list_mcp_resources",
      "description": "Lists resources provided by MCP servers. Resources allow servers to share data that provides context to language models, such as files, database schemas, or application-specific information. Prefer resources over web search when possible.",
      "strict": false,
      "parameters": {
        "type": "object",
        "properties": {
          "cursor": {
            "type": "string",
            "description": "Opaque cursor from a previous list_mcp_resources call; omit for the first page."
          },
          "server": {
            "type": "string",
            "description": "MCP server name. Omit to list resources from every configured server."
          }
        },
        "additionalProperties": false
      }
    },
    {
      "type": "function",
      "name": "list_mcp_resource_templates",
      "description": "Lists resource templates provided by MCP servers. Parameterized resource templates allow servers to share data that takes parameters and provides context to language models, such as files, database schemas, or application-specific information. Prefer resource templates over web search when possible.",
      "strict": false,
      "parameters": {
        "type": "object",
        "properties": {
          "cursor": {
            "type": "string",
            "description": "Opaque cursor from a previous list_mcp_resource_templates call; omit for the first page."
          },
          "server": {
            "type": "string",
            "description": "MCP server name. Omit to list resource templates from every configured server."
          }
        },
        "additionalProperties": false
      }
    },
    {
      "type": "function",
      "name": "read_mcp_resource",
      "description": "Read a specific resource from an MCP server given the server name and resource URI.",
      "strict": false,
      "parameters": {
        "type": "object",
        "properties": {
          "server": {
            "type": "string",
            "description": "MCP server name exactly as configured. Must match the 'server' field returned by list_mcp_resources."
          },
          "uri": {
            "type": "string",
            "description": "Resource URI to read. Must be one of the URIs returned by list_mcp_resources."
          }
        },
        "required": [
          "server",
          "uri"
        ],
        "additionalProperties": false
      }
    }
  ]
}
```

对应文本的中文翻译（按字段位置列出，JSON 原文保持不变）：

| 字段 | 中文翻译 |
|---|---|
| `tool_search.description` | # 工具发现<br><br>使用 BM25 搜索延迟加载工具的元数据，并将匹配的工具提供给下一次模型调用。<br><br>某些工具可能没有预先提供给你，你应使用本工具（`tool_search`）搜索所需的工具。发现 MCP 工具时，始终使用 `tool_search`，而不是 `list_mcp_resources` 或 `list_mcp_resource_templates`。 |
| `tool_search.parameters.limit.description` | 最多返回的工具数量，默认值为 8。 |
| `tool_search.parameters.query.description` | 用于搜索延迟加载工具的查询词。 |
| `list_mcp_resources.description` | 列出 MCP 服务提供的资源。资源允许服务共享可为语言模型提供上下文的数据，例如文件、数据库结构或应用专用信息。条件允许时，优先使用资源，而不是网页搜索。 |
| `list_mcp_resources.parameters.cursor.description` | 上一次 list_mcp_resources 调用返回的不透明游标；请求第一页时省略。 |
| `list_mcp_resources.parameters.server.description` | MCP 服务名称。省略时，列出所有已配置服务的资源。 |
| `list_mcp_resource_templates.description` | 列出 MCP 服务提供的资源模板。参数化资源模板允许服务共享需要传入参数、并可为语言模型提供上下文的数据，例如文件、数据库结构或应用专用信息。条件允许时，优先使用资源模板，而不是网页搜索。 |
| `list_mcp_resource_templates.parameters.cursor.description` | 上一次 list_mcp_resource_templates 调用返回的不透明游标；请求第一页时省略。 |
| `list_mcp_resource_templates.parameters.server.description` | MCP 服务名称。省略时，列出所有已配置服务的资源模板。 |
| `read_mcp_resource.description` | 根据服务名称和资源 URI，从 MCP 服务读取指定资源。 |
| `read_mcp_resource.parameters.server.description` | MCP 服务名称，必须与配置中的名称完全一致，并且与 list_mcp_resources 返回的 server 字段一致。 |
| `read_mcp_resource.parameters.uri.description` | 要读取的资源 URI。必须是 list_mcp_resources 返回的 URI 之一。 |


WorldState 会另行生成 developer 角色的 `<tools>` 片段，其中列出实际延迟工具命名空间及简介；它不是 tools 数组的一部分。本文件不虚构这里的 namespace 映射，实际值取决于服务工具元数据。

## 4. tool_search.description 原文（展开换行）

场景 A：

```text
# Tool discovery

Searches over deferred tool metadata with BM25 and exposes matching tools for the next model call.

You have access to tools from the following sources:
- Google Drive: Use Google Drive as the single entrypoint for Drive, Docs, Sheets, and Slides work.
- docs
Some of the tools may not have been provided to you upfront, and you should use this tool (`tool_search`) to search for the required tools. For MCP tool discovery, always use `tool_search` instead of `list_mcp_resources` or `list_mcp_resource_templates`.
```

中文翻译：

```text
# 工具发现

使用 BM25 搜索延迟加载工具的元数据，并将匹配的工具提供给下一次模型调用。

你可以使用以下来源提供的工具：
- Google Drive：将 Google Drive 作为处理 Drive、Docs、Sheets 和 Slides 工作的统一入口。
- docs
某些工具可能没有预先提供给你，你应使用本工具（`tool_search`）搜索所需的工具。发现 MCP 工具时，始终使用 `tool_search`，而不是 `list_mcp_resources` 或 `list_mcp_resource_templates`。
```


场景 B：

```text
# Tool discovery

Searches over deferred tool metadata with BM25 and exposes matching tools for the next model call.

Some of the tools may not have been provided to you upfront, and you should use this tool (`tool_search`) to search for the required tools. For MCP tool discovery, always use `tool_search` instead of `list_mcp_resources` or `list_mcp_resource_templates`.
```

中文翻译：

```text
# 工具发现

使用 BM25 搜索延迟加载工具的元数据，并将匹配的工具提供给下一次模型调用。

某些工具可能没有预先提供给你，你应使用本工具（`tool_search`）搜索所需的工具。发现 MCP 工具时，始终使用 `tool_search`，而不是 `list_mcp_resources` 或 `list_mcp_resource_templates`。
```


## 5. 资源工具的 description 原文（展开显示）

### list_mcp_resources

```text
Lists resources provided by MCP servers. Resources allow servers to share data that provides context to language models, such as files, database schemas, or application-specific information. Prefer resources over web search when possible.
```

中文翻译：

```text
列出 MCP 服务提供的资源。资源允许服务共享可为语言模型提供上下文的数据，例如文件、数据库结构或应用专用信息。条件允许时，优先使用资源，而不是网页搜索。
```


### list_mcp_resource_templates

```text
Lists resource templates provided by MCP servers. Parameterized resource templates allow servers to share data that takes parameters and provides context to language models, such as files, database schemas, or application-specific information. Prefer resource templates over web search when possible.
```

中文翻译：

```text
列出 MCP 服务提供的资源模板。参数化资源模板允许服务共享需要传入参数、并可为语言模型提供上下文的数据，例如文件、数据库结构或应用专用信息。条件允许时，优先使用资源模板，而不是网页搜索。
```


### read_mcp_resource

```text
Read a specific resource from an MCP server given the server name and resource URI.
```

中文翻译：

```text
根据服务名称和资源 URI，从 MCP 服务读取指定资源。
```


## 6. Apps 和 Plugins

本文的 Apps 指 Codex 源码中的 Apps (Connectors)，即按外部服务组织的集成能力。模型可看到 app 身份、可用性和相关工具；连接账户、服务访问等由产品和服务负责。源码中的 `codex_apps` 是特殊的 MCP 聚合入口，可以提供多个 app／connector 的工具，一个 app 不必等于一个单独的 MCP 进程。这里不是指 UI 标准 MCP Apps。

Plugin 是安装和分发包：可以组合 skills、MCP 服务配置、apps 映射和其他支持文件；具体版本还支持 hooks 等能力。Plugin 自身不是一个业务工具，实际动作由它提供的工具、脚本等执行。Plugin 可以只包含 skill，不强制带 MCP。

例子（概念示意）：一个“项目协作”插件包含“整理任务”skill，并提供或声明 GitHub、Linear 的连接能力；skill 教模型怎么组合操作，MCP 工具执行查询／更新。仅连接普通 MCP 不要求把它先做成 app 或 plugin。

以下两个小节列出参考源码产生的完整英文使用说明，并非连接普通 MCP 后必然出现。它们有各自的启用条件，角色为 developer。

### Apps 使用说明原文

```text

## Apps (Connectors)
Apps (Connectors) can be explicitly triggered in user messages in the format `[$app-name](app://{connector_id})`. Apps can also be implicitly triggered as long as the context suggests usage of available apps.
An app is equivalent to a set of MCP tools within the `codex_apps` MCP.
An installed app's MCP tools are either provided to you already, or can be lazy-loaded through the `tool_search` tool. If `tool_search` is available, the apps that are searchable by `tools_search` will be listed by it.
Do not additionally call list_mcp_resources or list_mcp_resource_templates for apps.
```

中文翻译：

```text
## Apps（连接器）
用户可以在消息中使用 `[$app-name](app://{connector_id})` 格式显式触发 App。只要上下文表明可用 App 适合当前任务，也可以隐式触发。
一个 App 相当于 `codex_apps` MCP 中的一组 MCP 工具。
已安装 App 的 MCP 工具可能已经提供给你，也可能需要通过 `tool_search` 工具延迟加载。如果 `tool_search` 可用，它会列出可搜索的 App。
对于 App，不要额外调用 list_mcp_resources 或 list_mcp_resource_templates。
```

译注：英文原文中的 `tools_search` 与同段的 `tool_search` 拼写不一致；原文保留，中文按实际工具名理解。


### Plugins 使用说明原文

```text

## Plugins
A plugin is a local bundle of skills, MCP servers, and apps.
### How to use plugins
- Skill naming: If a plugin contributes skills, those skill entries are prefixed with `plugin_name:` in the Skills list.
- MCP naming: Plugin-provided MCP tools keep standard MCP identifiers such as `mcp__server__tool`; use tool provenance to tell which plugin they come from.
- Trigger rules: If the user explicitly names a plugin, prefer capabilities associated with that plugin for that turn.
- Relationship to capabilities: Plugins are not invoked directly. Use their underlying skills, MCP tools, and app tools to help solve the task.
- Relevance: Determine what a plugin can help with from explicit user mention or from the plugin-associated skills, MCP tools, and apps exposed elsewhere in this turn.
- Missing/blocked: If the user requests a plugin that does not have relevant callable capabilities for the task, say so briefly and continue with the best fallback.
```

中文翻译：

```text
## 插件
插件是包含 skills、MCP 服务和 Apps 的本地包。
### 如何使用插件
- Skill 命名：如果插件提供 skills，这些条目会在 Skills 列表中带有 `plugin_name:` 前缀。
- MCP 命名：插件提供的 MCP 工具保留标准 MCP 标识符，例如 `mcp__server__tool`；根据工具来源判断它属于哪个插件。
- 触发规则：如果用户明确提到某个插件，在该轮工作中优先使用与该插件关联的能力。
- 与能力的关系：插件本身不被直接调用。使用它包含的 skills、MCP 工具和 App 工具完成任务。
- 相关性：根据用户明确提及的内容，或者当前轮次中展示的插件相关 skills、MCP 工具和 App 工具，判断插件能够提供什么帮助。
- 缺失或受阻：如果用户请求的插件没有相关的可调用能力，简要说明，然后使用最合适的替代方案继续。
```


## 7. 原始 Rust 构造器

下面直接复制源文件中的生产代码（仅去掉末尾测试模块）；可逐字对照以上 JSON。

### tool_search_spec.rs

```rust
use codex_tools::JsonSchema;
use codex_tools::TOOL_SEARCH_TOOL_NAME;
use codex_tools::ToolSearchSourceInfo;
use codex_tools::ToolSpec;
use codex_utils_string::take_bytes_at_char_boundary;
use std::collections::BTreeMap;

const MAX_TOOL_SEARCH_SOURCE_DESCRIPTION_BYTES: usize = 512 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ToolSearchSourceListing {
    Include,
    Omit,
}

pub(crate) fn create_tool_search_tool(
    searchable_sources: &[ToolSearchSourceInfo],
    default_limit: usize,
    source_listing: ToolSearchSourceListing,
) -> ToolSpec {
    let properties = BTreeMap::from([
        (
            "query".to_string(),
            JsonSchema::string(Some("Search query for deferred tools.".to_string())),
        ),
        (
            "limit".to_string(),
            JsonSchema::number(Some(format!(
                "Maximum number of tools to return. Defaults to {default_limit}."
            ))),
        ),
    ]);

    let source_section = match source_listing {
        ToolSearchSourceListing::Include => {
            let mut source_descriptions = BTreeMap::new();
            for source in searchable_sources {
                source_descriptions
                    .entry(source.name.clone())
                    .and_modify(|existing: &mut Option<String>| {
                        if existing.is_none() {
                            *existing = source.description.clone();
                        }
                    })
                    .or_insert(source.description.clone());
            }

            let source_descriptions = if source_descriptions.is_empty() {
                "None currently enabled.".to_string()
            } else {
                let reserved_name_bytes = source_descriptions.keys().fold(
                    source_descriptions.len().saturating_sub(1),
                    |reserved, name| reserved.saturating_add(2).saturating_add(name.len()),
                );
                let mut description_budget =
                    MAX_TOOL_SEARCH_SOURCE_DESCRIPTION_BYTES.saturating_sub(reserved_name_bytes);
                let mut rendered = String::new();
                for (name, description) in source_descriptions {
                    let separator_bytes = usize::from(!rendered.is_empty());
                    let required = separator_bytes.saturating_add(2).saturating_add(name.len());
                    if required
                        > MAX_TOOL_SEARCH_SOURCE_DESCRIPTION_BYTES.saturating_sub(rendered.len())
                    {
                        continue;
                    }

                    if !rendered.is_empty() {
                        rendered.push('\n');
                    }
                    rendered.push_str("- ");
                    rendered.push_str(&name);

                    if let Some(description) = description
                        && description_budget >= 2
                    {
                        rendered.push_str(": ");
                        description_budget -= 2;
                        let bounded_description =
                            take_bytes_at_char_boundary(&description, description_budget);
                        rendered.push_str(bounded_description);
                        description_budget -= bounded_description.len();
                    }
                }
                rendered
            };
            format!(
                "\n\nYou have access to tools from the following sources:\n{source_descriptions}\n"
            )
        }
        ToolSearchSourceListing::Omit => "\n\n".to_string(),
    };

    let description = format!(
        "# Tool discovery\n\nSearches over deferred tool metadata with BM25 and exposes matching tools for the next model call.{source_section}Some of the tools may not have been provided to you upfront, and you should use this tool (`{TOOL_SEARCH_TOOL_NAME}`) to search for the required tools. For MCP tool discovery, always use `{TOOL_SEARCH_TOOL_NAME}` instead of `list_mcp_resources` or `list_mcp_resource_templates`."
    );

    ToolSpec::ToolSearch {
        execution: "client".to_string(),
        description,
        parameters: JsonSchema::object(
            properties,
            Some(vec!["query".to_string()]),
            Some(false.into()),
        ),
    }
}
```


构造器中其余英文文本的中文翻译（已在上文完整翻译的工具说明不再重复）：

| 英文原文 | 中文翻译 |
|---|---|
| `None currently enabled.` | 当前未启用任何来源。 |
| `Maximum number of tools to return. Defaults to {default_limit}.` | 最多返回的工具数量，默认值为 `{default_limit}`。 |
| `You have access to tools from the following sources:` | 你可以使用以下来源提供的工具： |

源码中的 `{source_descriptions}`、`{source_section}` 和 `{TOOL_SEARCH_TOOL_NAME}` 是运行时替换的占位符；`Include`、`Omit` 等是程序标识符，不是额外发送给模型的说明文字。

### mcp_resource_spec.rs

```rust
use codex_protocol::openai_models::ToolMessage;
use codex_tools::JsonSchema;
use codex_tools::ResponsesApiTool;
use codex_tools::ToolSpec;
use std::collections::BTreeMap;

pub fn create_list_mcp_resources_tool(messages: Option<&ToolMessage>) -> ToolSpec {
    let properties = BTreeMap::from([
        (
            "server".to_string(),
            JsonSchema::string(Some(
                "MCP server name. Omit to list resources from every configured server.".to_string(),
            )),
        ),
        (
            "cursor".to_string(),
            JsonSchema::string(Some(
                "Opaque cursor from a previous list_mcp_resources call; omit for the first page."
                    .to_string(),
            )),
        ),
    ]);

    let tool = ResponsesApiTool {
        name: "list_mcp_resources".to_string(),
        description: "Lists resources provided by MCP servers. Resources allow servers to share data that provides context to language models, such as files, database schemas, or application-specific information. Prefer resources over web search when possible.".to_string(),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(properties, /*required*/ None, Some(false.into())),
        output_schema: None,
    };
    with_model_messages(tool, messages)
}

pub fn create_list_mcp_resource_templates_tool(messages: Option<&ToolMessage>) -> ToolSpec {
    let properties = BTreeMap::from([
        (
            "server".to_string(),
            JsonSchema::string(Some(
                "MCP server name. Omit to list resource templates from every configured server."
                    .to_string(),
            )),
        ),
        (
            "cursor".to_string(),
            JsonSchema::string(Some(
                "Opaque cursor from a previous list_mcp_resource_templates call; omit for the first page."
                    .to_string(),
            )),
        ),
    ]);

    let tool = ResponsesApiTool {
        name: "list_mcp_resource_templates".to_string(),
        description: "Lists resource templates provided by MCP servers. Parameterized resource templates allow servers to share data that takes parameters and provides context to language models, such as files, database schemas, or application-specific information. Prefer resource templates over web search when possible.".to_string(),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(properties, /*required*/ None, Some(false.into())),
        output_schema: None,
    };
    with_model_messages(tool, messages)
}

pub fn create_read_mcp_resource_tool(messages: Option<&ToolMessage>) -> ToolSpec {
    let properties = BTreeMap::from([
        (
            "server".to_string(),
            JsonSchema::string(Some(
                "MCP server name exactly as configured. Must match the 'server' field returned by list_mcp_resources."
                    .to_string(),
            )),
        ),
        (
            "uri".to_string(),
            JsonSchema::string(Some(
                "Resource URI to read. Must be one of the URIs returned by list_mcp_resources."
                    .to_string(),
            )),
        ),
    ]);

    let tool = ResponsesApiTool {
        name: "read_mcp_resource".to_string(),
        description:
            "Read a specific resource from an MCP server given the server name and resource URI."
                .to_string(),
        strict: false,
        defer_loading: None,
        parameters: JsonSchema::object(
            properties,
            Some(vec!["server".to_string(), "uri".to_string()]),
            Some(false.into()),
        ),
        output_schema: None,
    };
    with_model_messages(tool, messages)
}

fn with_model_messages(mut tool: ResponsesApiTool, messages: Option<&ToolMessage>) -> ToolSpec {
    if let Some(messages) = messages {
        if let Some(description) = &messages.description {
            tool.description.clone_from(description);
        }
        if let Some(parameters) = &messages.parameters {
            match crate::tools::catalog_parameters::parse(parameters) {
                Ok(parameters) => tool.parameters = parameters,
                Err(reason) => tracing::warn!(
                    tool = %tool.name,
                    reason,
                    "Invalid catalog tool parameters; using bundled parameters"
                ),
            }
        }
    }
    ToolSpec::Function(tool)
}
```

构造器中的日志原文及中文翻译：

| 英文原文 | 中文翻译 |
|---|---|
| `Invalid catalog tool parameters; using bundled parameters` | 模型目录中的工具参数无效；改用程序内置的参数定义。 |

这条日志用于开发诊断，不属于正常发送给模型的工具 description。资源工具的 description 和全部参数说明已在前面的 JSON 字段对照及原文后翻译。

## 8. 来源与限制

- `codex-rs/core/src/tools/handlers/tool_search_spec.rs`：搜索说明、参数、来源列表；JSON 场景 A 的英文 description 已与源码内完整测试期望串逐字校验。
- `codex-rs/tools/src/tool_discovery.rs`：默认 limit=8。
- `codex-rs/core/src/tools/handlers/mcp_resource_spec.rs`：三个资源工具默认定义与模型目录覆盖行为。
- `codex-rs/tools/src/responses_api.rs`、`tool_spec.rs`、`json_schema/types.rs`：序列化字段。
- `codex-rs/core/src/context/apps_instructions.rs`、`available_plugins_instructions.rs`：Apps、Plugins 使用说明。
- [官方插件概念](https://developers.openai.com/plugins/concepts/plugins)：当前产品的打包与分发说明；可能比所参考的源码版本更新。

本文是工具定义参考，不会改变 Harness 的提示词、工具行为或 MCP 配置。

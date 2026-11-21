# Aster Images

> 新增 [Windows 原生运行说明](WINDOWS.md)。

独立的生图、改图 MCP，委托本机官方 Codex 的内置图片工具，使用其 ChatGPT 登录额度。只提供 `image_generate`、`image_edit`，不依赖 Harness、Squad、Relay，不转图片 API，不自动重试或迭代。

## 运行

```sh
python -m pip install .
codex login
# 宿主私有服务：先在环境中设置随机 ASTER_IMAGES_TOKEN（至少 24 字符）
aster-images serve --root /private/image-jobs --model YOUR_CODEX_MODEL
# 成员侧标准 stdio MCP：同一个 ASTER_IMAGES_TOKEN 和服务地址
ASTER_IMAGES_URL=http://127.0.0.1:19931 aster-images mcp
```

`serve` 默认监听 loopback 19931，启动时要求 Codex 为 ChatGPT 登录。`--codex` 可指定可执行文件，`--codex-home` 指定其登录目录，默认读取 CODEX_HOME 或 ~/.codex；未指定模型时使用官方 Codex 默认模型。服务环境和日志由宿主管理，需隐藏于成员的文件权限之外。不要把凭据或任务日志放进源码仓库。

两层进程有明确用途：私有服务持有 Codex 登录、串行执行出图；stdio MCP 在调用者自己的文件沙箱中读取输入图、上传字节、保存输出图。服务不接受宿主文件路径，不能拿它绕过调用者的文件权限。无文件沙箱的独立客户端按启动者的普通文件权限运行。

| 工具 | 参数 |
| --- | --- |
| `image_generate` | prompt、可选 reference_paths（最多 4 张）、transparent_background=false、output_dir="images" |
| `image_edit` | prompt、image_path、可选 reference_paths（最多 3 张）、transparent_background=false、output_dir="images" |

prompt 1–8000 字符；图片为 PNG/JPEG/WebP/GIF，单张最多 8 MiB、2500 万像素，合计最多 16 MiB。路径相对当前工作区解析；输出目录必须位于工作区。编辑产生新 PNG，不覆盖原图。返回元数据与标准 MCP 图片块，支持图片输入的模型可直接看到结果；供应商能力与 agent 推理模型无关。

## 执行与限制

每次请求启动 `codex exec --ephemeral --ignore-user-config --ignore-rules --json`，使用只读命令沙箱，关闭 shell、unified_exec、apps 和网页搜索。参考图片通过 CLI 图片附件传入；Codex 仍需解释任务并决定内部工具参数，没有强制逐字透传接口。尺寸、构图、保持主体等是生成要求，不保证像素精确；透明背景要求会检查结果的实际 alpha 通道。失败不会自动再生成。

当前验证 Codex CLI 0.160.0：exec 的 JSONL 没有直接输出 imageGeneration 事件。服务以 `thread.started` 回执锁定本次新 Thread，只接受其 `CODEX_HOME/generated_images/<thread_id>/` 中唯一的原生 PNG，并校验文件；不相信助手文字声称的任意文件路径。文件目录契约随版本可能变化，需重新验证版本升级。

一次最多运行一个任务，忙时明确返回 busy 且不启动任务。超时停止本次独立 Codex 进程组；可能已经消耗额度，不代表服务端图像任务一定撤销。不要盲目重试。日志、输入与最终图片保存在私有任务目录，普通会话 rollout 不持久化；没有自动清理历史或生成大批图片。

登录只由宿主配置。不会把 Codex 登录凭据、上游 API key 或私有日志交给 MCP 调用者。原生生成文件由 Codex 另外保存，服务不修改全局 Codex 配置。

## 接入 Squad

安装本包后，将以下条目加入宿主 MCP 目录，command 改成安装环境中 `aster-images` 的绝对路径。服务端和成员连接使用同一个随机 token，宿主环境提供这两个变量。

```json
{
  "images": {
    "name": "图片生成与编辑 · Codex 订阅",
    "default": false,
    "server": {
      "command": "/path/to/venv/bin/aster-images",
      "args": ["mcp"],
      "env_vars": ["ASTER_IMAGES_URL", "ASTER_IMAGES_TOKEN"],
      "required": false,
      "tool_timeout_sec": 480
    }
  }
}
```

复制或注册包内的 `aster_images/assets/skills/images/SKILL.md` 至宿主共享 skill 目录。成员工作区不预置平台文件；图片只在实际调用时生成。目录加载后可在工作台按成员启用。

## 验证

```sh
python -m unittest discover -s tests -v
```

自动测试使用假 Codex 和 loopback HTTP/MCP，不消耗订阅额度。真实验收需另行配置 Codex 登录，并明确控制调用次数；建议覆盖生图、改图、Harness 沙箱 → stdio MCP → HTTP worker → Codex 的完整链路，以及输入文件权限、源图保留和凭据隔离。

参考：[Codex 图片生成](https://learn.chatgpt.com/docs/image-generation)、[非交互执行](https://learn.chatgpt.com/docs/non-interactive-mode)。暂未指定许可证。

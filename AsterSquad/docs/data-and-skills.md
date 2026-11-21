# 数据目录与 skills

工作台和独立 Squad 默认数据根目录为 `~/.aster-squad/`；优先级是 `--root`、`ASTER_SQUAD_HOME`、默认值。源码、安装包与数据分开。已有 `./data` 不会被启动命令自动移动或删除，迁移需显式执行。

为保持既有 Thread 和空间标识，当前沿用内部 slot 布局：

```text
<root>/
  control/                         # 工作台配置、数据库与认证
  skills/global/                   # 所有空间共享，内置指南随部署更新
  skills/sources.json              # 可选：宿主注册的额外全局来源
  skills/space/                    # 默认空间指南
  skills/agents/<agent-id>/         # 默认空间成员的专属指南
  connections/slot-XX/              # 自己的 Relay 连接与监听脚本
  runtime/slot-XX/threads/          # Harness Thread 与持久运行状态
  workspaces/slot-XX/               # 成员自行支配
  instances/slot-XX/                # 其他空间，各有 control、skills、connections、runtime、workspaces
```

`instances/slot-XX/skills/space/` 是该空间的共享来源；`skills/agents/<agent-id>/` 是该空间成员的专属来源。全局来源只保存一份。空间启动参数 `--shared-skill-root` 由工作台设置，独立 Squad 不设置时以自己的 root 为共享根。

系统不在 workspace 创建 skills、项目脚手架、Relay 配置或监听脚本。正式项目放在外部，通过路径 grants 授权。旧 workspace 中已存在的内容属于成员，不批量删除或覆盖。旧程序发生器可继续引用原有脚本。

## 发现与权限

自动注册顺序为全局、空间、个人，然后宿主额外来源。目录摘要含名称、描述、SHA-256 和 source 范围，正文、references 用 `skills.read` 按需读取；不把所有文档注入 instruction。所有来源通常被普通工具的文件沙箱禁止读取，专用读取只允许所选 skill 包内的文本，不执行代码。

Squad 关闭 Harness 的 workspace/skills 自动扫描。成员仍可对自己普通文件权限可读的来源调用 `skills.load`。动态来源的扫描及资源读取遵守沙箱，包括恢复后、权限收回后；不能借动态加载读取宿主私有目录。独立 Harness 默认保留 workspace skills 发现，可用 `[skills] workspace = false` 关闭。

同名 skill 先注册来源优先，目录诊断显示冲突和保留的来源，不静默覆盖。个人自建同名指南应改名或由维护者调整来源。来源更新会在下一次模型步骤发现；没有新事件时不会仅因改动文档唤醒模型。

额外来源由宿主在 `skills/sources.json` 注册，例如：

```json
[{"path": "/absolute/project/skills", "label": "local-operations"}]
```

这个清单本身位于保护目录，agent 不能靠编辑它绕过权限。更新清单后需暂停/启动相关成员以更新来源和沙箱掩码；现有已注册 skill 包的文本更新则不需要重启。外部来源默认 deny；某成员被明确授予覆盖该来源的 write grant 时，可维护该目录。文档维护授权不附带宿主、网关或服务管理授权。

`aster-harness` 来源由 Harness 包维护，`squad` 由 Squad 包维护，`relay` 由 Relay 包维护；部署时同步到全局目录。升级时，已确认的旧内置 `aster-squad` 包会归档到受保护的 `control/retired-skills/`，其文档合并至 `squad`；若入口被定制则要求先审阅，不自动覆盖或删除。不要直接编辑这些内置包的安装副本，修改各项目的源文件后发布。新增自定义全局指南不受内置同步影响。本机域名和端口规则应注册为私有外部来源，不提交到公共仓库。

## 显式迁移

`python -m squad.migrate --source /old/data --destination ~/.aster-squad` 默认预览；加 `--apply` 才执行。先正常暂停成员并停止工作台，保留完整备份。默认备份放在新 root 的 `control/migration-backups/` 内，沿用控制目录的沙箱保护；如显式选择外部备份目录，部署者必须同样将其设为不可访问。迁移只更新当前配置与数据库中的部署路径，不改写历史、记忆或 workspace 文件。旧 root 保留兼容符号链接，既有历史和脚本里的绝对路径继续解析；这不是第二份运行数据。移除兼容链接前必须排查旧路径引用。

迁移工具不管理 systemd、反向代理或外部脚本；部署者更新启动参数和凭据文件位置后重启，恢复迁移前在线的同一批 Thread。路径变化不改变模型、成员 ID、聊天账号、密码或通行密钥。

# 项目边界与部署约定

依赖方向是 Squad → Aster Harness、Relay；Harness 与 Relay 互不依赖，也不依赖 Squad。Aster Web 与 Aster Images 是可选 MCP 服务，不属于 Harness 核心。平台专用监听桥接放在集成方，不能反向进入基础项目。

仓库保留代码、公开接口、通用 CLI、示例和验证。账号、token、真实域名/IP、端口转发、VPN、个人项目与生产服务配置放在部署者的数据目录或独立部署项目。GitHub 仓库地址是项目元数据；loopback、产品默认端口、系统标准路径和 example.test 是可移植的默认值或示例，不是本机部署记录。

安装与数据目录独立；Linux 与 Windows 各自使用本机路径、账号和运行数据，不直接共享正在使用的 SQLite 或 Thread。跨平台统一管理语义、状态和错误，不伪装 OS 能力相同。root/UAC 的系统授权、服务托管和设备访问由对应平台负责。

新增平台分支只放在对应执行/认证适配边界；公共功能不散布下游应用名或私人部署条件。Squad 通过公开 SDK、CLI 和 HTTP/MCP 集成；不读取 Harness 的 private core/experimental 接口。文件越长不等于必须拆文件；按稳定职责和调用边界调整，避免为了统一而再造框架。

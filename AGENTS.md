# Repository Guidelines

## 项目结构与模块组织

BotSleep 基于 `mc-agent-api` 控制 Minecraft 机器人。当前 `main` 分支已切流至 **BotSleep v2**（L0–L4 单向五层行为树微内核架构）；旧版 v1 代码永久归档于 Git 分支 `archive/v1` 与标签 `archive/v1-final`（`c6859dc`）。

- **用户约束（2026-11-16）**：允许拉取相邻 `mc-agent-api` 的官方更新；不要在该仓库修改或提交本地源码补丁。所有适配和修复在 `BotSleep` 内完成；上游行为不足时记录限制，不依赖本地 API 补丁。

- `main.py`、`bot_sleep.py`、`client.py`：配置驱动的入口与兼容薄壳，转发至 `v2/app.py` 与 `v2/kernel/client.py`，不要复制实现。`survival.objective` 区分 `starter`、`daily` 与 `competition`，默认运行自动睡眠；开启 `llm.enabled` 后使用原生工具调用与结果反馈。
- `v2/domain.py`：共享领域常量、朝向与坐标变换、四朝向 `StructureSpec`、威胁半径与方块分类。
- `v2/kernel/`（L0）：`config.py` 配置加载、`client.py`（单线程写 Session 与 `threading.local()` 只读连接池）、`storage.py` + `journal.py`（作用域 WAL SQLite、`PendingTransactionJournal`、跨进程实例锁）与 `model_gate.py`（串行请求并发=1、30s 传输恢复窗口）。
- `v2/gateway/`（L1）：`world_proxy.py` 三源融合体素缓存与精确射线遮挡检测、`unreachable.py` TTL 黑名单、`pathfinder_3d.py` 3D A*、`multihop_navigator.py` 与 `surface_swim.py`。
- `v2/bt/`（L2）：反应式行为树微内核、`SyncBlackboard`、`LoopWatchdog` 与抢占优先级分支；严格约束 `Node.stop(bb)` 与 `Task.stop(bb)` 为纯内存状态复位，禁止在 `stop()` 内发起网络写。
- `v2/primitives/` & `v2/tasks/`（L3）：8 类正交单写原语、`TaskCatalogue`（886 配方 DAG）、无状态递归生存/矿井/建造/门户任务，以及 `v2/tasks/competition/`（45 无条件 + 54 条件 override 计分门禁，服务端 `/advancements` 唯一计分真值）。
- `v2/planner/` & `skills_kb/`（L4）：`DepsPlanner`、`HorizonSelector`、15 张 `skills_kb/*.md` 零依赖 BM25 召回、`SemanticCompactor`（`<= 6.6KB`）与非阻塞异步 `SerialModelWorker`。
- `v2/observability/`：脱敏结构化 `FlightRecorder`、`MonitorServer` 本机只读监控与聊天调试。
- `scripts/`、`integrations/ninjabrain/`、`docs/`、`tests_v2/`：只读运维检查 `scripts/botctl.py`、多机分散压测 `scripts/multi_bot_runner.py`、无界面要塞计算 `integrations/ninjabrain/`（synthetic data 在 `data/examples/`）、架构与测试文档及标准库 `unittest` 回归集。禁止在 `v2/`、`scripts/`、`integrations/`、入口文件或 `tests_v2/` 中反向跨层导入或引入已归档旧模块（由 `tests_v2/test_00_layer_boundaries.py` 与 `tests_v2/test_10_ops_and_cli.py` AST 守护）。
- 不得把蓝图或离线回归说成实服执行验收；unknown 写入跨重启冻结、禁止自动重放。`llm.model` 是请求路由，实际模型仅采用 `response.model`，缺失时显示“未确认”。

## 构建、测试与开发命令

在仓库根目录使用 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe main.py --check-config
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe bot_sleep.py
```

前两步创建环境并安装当前代码依赖；两个启动命令复用同一配置驱动入口，默认自动睡眠。运行前需启动 Minecraft 服务及 `mc-agent-api`，默认接口为 `http://127.0.0.1:18900/api/v1`。模型规划需同时设置 `agent.mode: survival` 与 `llm.enabled: true`；`debug.monitor.enabled: true` 启用默认 `127.0.0.1:18930` 的只读监控。

`v2/kernel/config.py` 读取 YAML，支持环境变量和用户名命令行覆盖。正常运行只依赖 `requests` 和 `PyYAML`；RCON 为可选开发依赖（`requirements-dev.txt`）。配置、客户端、原语与任务接口应同步维护。

## 编码风格与命名约定

沿用 Python 四空格缩进；模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。公共接口添加类型注解与简短文档字符串。HTTP 交互集中在 L0 `v2/kernel/client.py`，环境/寻路封装放入 L1 `v2/gateway/`，行为树策略放入 L2 `v2/bt/`，组合动作放入 L3 `v2/primitives/` 与 `v2/tasks/`，模型决策放入 L4 `v2/planner/`。禁止新增代码违反 L0→L4 单向依赖或引入已移除的旧模块。当前未配置格式化或 lint 工具，避免无关格式改动。

## 测试与验证

离线行为测试使用标准库 `unittest`，不连接真实游戏。提交前运行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2 -v
```

新增或修改行为时，补充对应 `tests_v2/` 单元测试（使用 `VirtualMcApiServer` 或局部 mock，不连实网/真游戏）。涉及真实游戏行为时，在专用重置测试世界验证，并严格区分：离线单元测试通过、单步技能成功、有限目标达成与完整实服端到端验收。不得把跳过或离线模拟记为实服通过。

要塞计算引擎另需 JDK 11+；执行 `python -m integrations.ninjabrain setup` 下载并校验固定版本 JAR 后再测试。缺少 JAR 时引擎用例会显式跳过，报告必须区分通过和跳过，不能算作完整引擎验证。

## 提交与拉取请求

沿用语义化提交前缀（`feat:`、`fix:`、`refactor:`、`docs:`、`chore:`、`test:`），概括单一变更。拉取请求应说明目的、影响层级/模块、验证命令和结果；涉及实服行为时附日志与实测范围说明，并同步更新架构或测试文档。

## 配置与数据安全

不要提交真实 API 密钥、机器人密码或 RCON 凭据。`config.yaml`、`.env`、`.venv/`、`logs/`、`runtime/` 和 `scratch/` 已被忽略。`data/game_logic/` 中的参考文件受版本控制，提交前检查是否混入本地运行状态或私人服务器信息。

计算与采样说明见 `docs/STRONGHOLD.md`。纯计算不连接游戏；`capture` 会消耗末影之眼并登录后登出。生成的 `.cache/ninjabrain/` 不提交，上游许可证见 `third_party/ninjabrain/`。

## Harness AI Skill 编写约定

后续 skill / `skills_kb/*.md` 默认面向小模型：

- 正文简短，以明确目标、调用表和不超过五步的流程为主；默认争取约 300～600 中文字，必要接口示例可适量超出。
- 接口写明真实名称或 HTTP 方法/路径、参数示例、单位及关键返回字段。尚未绑定的入口必须注明，不虚构可调用工具。
- 原理、公式、来源和长示例放入 `references/`，不默认注入模型上下文。只给当前任务需要的状态与结果摘要。
- 会话管理、坐标转换、采样、数学计算及并发监控由 harness 负责；skill 只让模型选择行动，并给出明确的完成、失败和中止条件。

## 文档状态同步

- `docs/ARCHITECTURE.md` 描述当前 v2 运行链路；`docs/LIVE9_90M_REPORT_20261126.md` 与 `docs/TESTING.md` 统一维护最新离线回归与实服测试证据。
- `docs/HARNESS_INTERFACES.md` 与 `docs/SURVIVAL_PLAYBOOK.md` 仍记录能力边界与未开放目标；不得将未实现能力宣传为已实服打通。
- 测试结果注明源码基线、环境与是否本轮复核；历史全绿不能覆盖另一环境中的失败。最新结果与已知差异统一放 `docs/TESTING.md`。

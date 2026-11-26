# BotSleep

基于 `mc-agent-api` 的 Minecraft 机器人控制系统。自 **BotSleep v2** 起采用 **L0–L4 单向分层行为树微内核** 架构（`v2/`、`skills_kb/`、`tests_v2/`），支持自动睡眠、常规生存自主规划（`starter` / `daily`）与服务端 advancement 计分赛事模式（`competition`）。

> 所有适配和修复在 `BotSleep` 内完成，严禁修改或提交相邻 `mc-agent-api` 仓库源码；上游行为不足时记录限制，不依赖本地 API 补丁。

## 核心架构（L0–L4 单向依赖）

详细设计见 [REWRITE_V2_MASTERPLAN.md](docs/REWRITE_V2_MASTERPLAN.md) 与 [ARCHITECTURE.md](docs/ARCHITECTURE.md)，模块划分如下：

| 层级与目录 | 职责 |
|---|---|
| `main.py`、`bot_sleep.py`、`client.py` | 主入口、兼容启动壳与客户端兼容导出（均转发至 `v2` 实现） |
| `v2/domain.py` | 共享领域常量、朝向/坐标变换、`StructureSpec` 与危险判定 |
| `v2/kernel/`（L0） | 配置加载、`AsyncReadPool` + 单线程写 HTTP 客户端、WAL `UnifiedWorldDB`、`PendingTransactionJournal`、实例锁与主/备双路串行 `ModelRequestGate` |
| `v2/gateway/`（L1） | 三源融合 `VirtualWorldGateway` 体素缓存、`Pathfinder3D`、`MultiHopNavigator`、`SurfaceSwim` 与 TTL `unreachable` 黑名单 |
| `v2/bt/`（L2） | 行为树微内核：`SyncBlackboard`、`LoopWatchdog`、安全抢占（`PendingUnknownGuard > HardSafety > LocalRecovery > ActiveGoal > AsyncReplan`），严格保证 `Node.stop()` 纯内存零写 |
| `v2/primitives/` & `v2/tasks/`（L3） | 8 类正交单写原语、`TaskCatalogue`（886 配方 DAG）、无状态递归生存任务与 `v2/tasks/competition/` 赛事执行/计分 |
| `v2/planner/` & `skills_kb/`（L4） | `DepsPlanner`、`HorizonSelector`、15 张 `skills_kb/*.md` 零依赖 BM25 召回、`SemanticCompactor`（`<= 6.6KB`）与非阻塞 `SerialModelWorker` |
| `v2/observability/` | 脱敏 `FlightRecorder`、`MonitorServer` 本机只读监控与聊天调试 |
| `scripts/`、`integrations/ninjabrain/` | 只读运维检查 `botctl.py`、多机分散出生压测器 `multi_bot_runner.py`、要塞计算与采样模块 |

## 跨机器环境一致性清单（推荐先核对）

为避免不同电脑上跑出环境、编码、旧状态或依赖差异，请统一遵循以下基线：

- **Python 与依赖版本**：使用 Python `3.10 – 3.13` 独立虚拟环境 `.venv`，严格从 [requirements.txt](requirements.txt) 安装（`requests>=2.31,<3`、`PyYAML>=6,<7`；可选 RCON 依赖见 [requirements-dev.txt](requirements-dev.txt)），不要依赖全局 `pip` 环境。
- **终端与文件 UTF-8 编码**：Windows 终端建议设置 `$env:PYTHONUTF8="1"`（Linux/macOS 默认 UTF-8，可选 `export PYTHONUTF8=1`），避免系统默认编码（如 GBK）导致中文日志或 JSON 读写异常。
- **外部运行基线**：相邻 `mc-agent-api` `1.3.19`（保持原版源码零改动，默认监听 `http://127.0.0.1:18900/api/v1`）+ Minecraft Java `1.21.11`（实服基线 Leaves `1.21.11`、`difficulty=easy`）。
- **状态与存档严格隔离**：`survival.state_dir` 按 `(endpoint, username, world_id)` 持久化 WAL 数据库与 `pending` 锁。**每次更换或重置 Minecraft 世界，必须修改 `survival.world_id` 或清理旧 `survival.state_dir`**，否则旧方块记忆/黑名单/锁会干扰新世界。
- **环境变量优先级排查**：环境变量会覆盖 YAML，跨机出现行为不一致时先检查本机是否残留以下变量：
  `BOTSLEEP_CONFIG`、`MC_AGENT_API`、`MC_AGENT_USER`、`MC_AGENT_PASSWORD`、`BOTSLEEP_LLM_BASE_URL`、`BOTSLEEP_LLM_API_KEY`、`BOTSLEEP_LLM_MODEL`、`BOTSLEEP_LLM_OVERFLOW_BASE_URL`、`BOTSLEEP_LLM_OVERFLOW_API_KEY`、`BOTSLEEP_LLM_OVERFLOW_MODEL`。
- **老工作区缓存清理**：若从旧版 v1 `git pull` 升级，清理旧目录残余未追踪的 `__pycache__/` 空文件夹即可。

> **安全注意**：真实 API 密钥、机器人口令与 RCON 凭据仅存本地 `config.yaml` 或环境变量，切勿提交入库；`config.yaml`、`.env`、`.venv/`、`logs/`、`runtime/` 均在 `.gitignore` 中忽略。

## 安装、校验与运行

### 1. 创建隔离环境并校验配置

Windows（PowerShell）：

```powershell
$env:PYTHONUTF8="1"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe -B main.py --config config.yaml --check-config
```

Linux / macOS（Bash）：

```bash
export PYTHONUTF8=1
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
./.venv/bin/python -B main.py --config config.yaml --check-config
```

`--check-config` 不连接游戏、API 或大模型；成功时输出 `Configuration valid.`。

### 2. 启动运行（需先启动 Minecraft 服务端与 `mc-agent-api`）

以下命令将 `<PY>` 替换为 `.\.venv\Scripts\python.exe`（Windows）或 `./.venv/bin/python`（Linux/macOS）：

```powershell
# 默认自动睡眠；单次有界尝试可加 --once
<PY> -B main.py --config config.yaml

# 初期石器 / 日常生存 / 赛事（competition 需启用 llm.enabled 并关闭聊天调试）
<PY> -B main.py --config config.yaml --mode survival --objective starter
<PY> -B main.py --config config.yaml --mode survival --objective daily
<PY> -B main.py --config config.yaml --mode survival --objective competition

# 存在 pending 未知写动作或需断线续跑时
<PY> -B main.py --config config.yaml --mode survival --resume

# 退出后保持连接（不主动登出）
<PY> -B main.py --config config.yaml --keep-connected

# 只读检查实例锁、pending 与模型门禁状态（不连网）
<PY> -B scripts/botctl.py --config config.yaml status

# 多 Bot 分散出生并发压测（需配置好服务端后使用）
<PY> -B scripts/multi_bot_runner.py --help
```

配置覆盖顺序：
- 配置文件：`--config` > `BOTSLEEP_CONFIG` > `config.yaml`
- 账号：`--username` > `MC_AGENT_USER` > YAML > 默认值
- API 与口令：`MC_AGENT_API` / `MC_AGENT_PASSWORD` > YAML > 默认值
- 模型网关：`BOTSLEEP_LLM_BASE_URL` / `BOTSLEEP_LLM_API_KEY` / `BOTSLEEP_LLM_MODEL` > YAML > 默认值（全部参数见 [CONFIGURATION.md](docs/CONFIGURATION.md)）

## 离线测试与实服基准

离线全量单元测试仅使用 Python 标准库 `unittest` 与纯内存仿真 `VirtualMcApiServer`，**不连接真实 Minecraft、网络或大模型 API**，在任何系统上运行结果应完全一致：

```powershell
# Windows (PowerShell)
.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2 -v

# Linux / macOS (Bash)
./.venv/bin/python -B -m unittest discover -s tests_v2 -v
```

- **预期通过标准**：输出 **`Ran 78 tests ... OK`**（`0` failed / `0` errors / `0` skipped）。
- **测试范围**：AST 单向层边界守护（`test_00`、`test_10`）、仿真服务器（`test_01`）、L0–L4 分层回归、反卡死/举盾迟滞（`test_08`）、10 格模具保护（`test_09`）与运维/要塞助手（`test_10`）。
- **当前验证基线（2026-11-26，详情见 [TESTING.md](docs/TESTING.md) 与 [LIVE9_90M_REPORT_20261126.md](docs/LIVE9_90M_REPORT_20261126.md)）**：
  - 离线 `tests_v2` **`78 / 78` 通过**，`v2/` 全量 63 文件行覆盖率 **`80.81%`**（`6,690 / 8,279` 行）。
  - 实服 9-Bot（`SleepBot1..SleepBot9`）`1.5h`（5400s）双 API 压测全员 **`9/9 rc=0`** 零未捕获异常退出；前排 `+35.5m / +36.3m` 达成铁锭/铁镐，双 API 平稳分流；该轮进下界仍为 `0/9`，水边震荡与铁后下界过渡仍在持续优化。

要塞计算引擎（`integrations/ninjabrain/`）另需 JDK 11+：

```powershell
<PY> -m integrations.ninjabrain setup
<PY> -m integrations.ninjabrain calculate --input data/examples/stronghold_throws.json
```

## 旧版本归档与拉取升级说明

BotSleep v1 终态代码与各已合入/收口历史分支均已转为 Git 标签永久保留（仓库分支列表仅保留活动主干/开发分支，避免闭合分支杂乱显示）：
- **v1 终态归档标签**：`archive/v1-final`（指向 `c6859dc`）
- **历史功能节点标签**：`archive/rewrite-v2`、`archive/api-model-opt`、`archive/ai-iron-diamond-loop`、`archive/shelter-workflow`、`archive/portal-workflow`、`node/web-view-20261119`

协作者从旧版 `main` 同步至当前新版时，直接执行常规拉取即可（非强推历史）：

```powershell
git checkout main
git pull
```

- Git 会**自动删除**旧版根目录 `agent/`、`core/`、`debug/`、`skills/`、`tests/` 及 `probe_api.py`，并拉取新版 `v2/`、`skills_kb/` 与 `tests_v2/`。
- 若本机此前运行过旧代码，旧目录下可能残留未被 Git 追踪的 `__pycache__/` 或本地运行产物；如有空目录残留，可安全手动删除。
- 如需查阅或对比旧版源码，可切换至归档标签（`git checkout archive/v1-final`）。

## 文档导航

- **[文档总目录（区分现行 v2 与历史归档 v1）](docs/README.md)**
- [架构与运行链路](docs/ARCHITECTURE.md) / [v2 重构总方案](docs/REWRITE_V2_MASTERPLAN.md) / [开发执行计划](docs/REWRITE_V2_AGENT_PLAN.md)
- [配置与赛事参数指南](docs/CONFIGURATION.md)
- [测试记录](docs/TESTING.md) / [9-Bot 90 分钟与双 API 压测报告](docs/LIVE9_90M_REPORT_20261126.md)
- [要塞计算与采样](docs/STRONGHOLD.md) / [赛事规则目录说明](data/competition/README.md)

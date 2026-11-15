# BotSleep

基于 mc-agent-api 的 Minecraft 机器人控制项目，采用模型决策与本地技能分层设计。默认运行自动睡眠；生存入口支持初期石器目标（starter）、日常维护（daily）和赛事得分任务（competition）。默认不调用模型，正常运行无需 RCON。

当前生存主链路已接通观察、规划、技能执行、结果验证与反馈。独立合成、伐木、雷达和地狱门开发技能的存在，不代表它们已进入生存模型的工具目录或通过完整实服验收。验证基线、环境、通过与跳过情况统一见 [测试记录](docs/TESTING.md)，历史测试数字不作为最新合并版本的结果。

## 安装与运行

需要 Python 3.10+。在仓库根目录使用 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe -B main.py --config config.yaml --check-config
```

编辑 `config.yaml` 中的 `mc_api.base_url`、`agent.name` 和认证配置，使其与服务端匹配。默认 API 地址为 `http://127.0.0.1:18900/api/v1`；运行前需启动 Minecraft 服务端和 mc-agent-api。`--check-config` 只校验配置，不连接游戏、API 或模型。

```powershell
# 默认自动睡眠；单次有界尝试可加 --once
.\.venv\Scripts\python.exe -B main.py --config config.yaml

# 初期石器目标
.\.venv\Scripts\python.exe -B main.py --config config.yaml --mode survival --objective starter

# 日常生存维护
.\.venv\Scripts\python.exe -B main.py --config config.yaml --mode survival --objective daily

# 赛事任务：还需 llm.enabled: true，并关闭聊天调试
.\.venv\Scripts\python.exe -B main.py --config config.yaml --mode survival --objective competition
```

`bot_sleep.py` 复用 `main.py`；根目录 `client.py` 仅兼容导出 `core/client.py`，不维护第二份实现。

配置覆盖规则：

- 配置路径：`--config` > `BOTSLEEP_CONFIG` > 项目中的 `config.yaml`。
- 用户名：`--username` > `MC_AGENT_USER` > YAML > 默认值。
- API 地址与口令：`MC_AGENT_API` / `MC_AGENT_PASSWORD` > YAML > 默认值。
- 模型地址、密钥与请求路由：`BOTSLEEP_LLM_BASE_URL` / `BOTSLEEP_LLM_API_KEY` / `BOTSLEEP_LLM_MODEL` > YAML > 默认值。
- `--mode`、`--objective` 可覆盖对应配置；日志目录相对配置文件解析。

真实 API 密钥、机器人口令和 RCON 凭据只放本地配置或环境变量。`config.yaml`、`.env`、`.venv/`、`logs/` 不入库；游戏存档与代码仓库隔离。受版本控制的 `data/game_logic/` 不用于当前运行检查点，也不要写入私人服务器信息。

## 运行链路与能力边界

| 入口 | 当前执行范围 | 完成依据与限制 |
| --- | --- | --- |
| sleep（默认） | 找床、接近、交互、等待醒来 | 检查维度、床占用与附近敌人；须观察到入睡到醒来，或服务端 slept 证据与昼夜/雷暴转换。下界、末地拒绝用床。 |
| survival / starter | 木材、工作台、木镐、圆石及初期工具制作 | 目标为石镐、石斧、石剑和熔炉；以当前背包与设施验证，不以模型自报完成。 |
| survival / daily | 13 项日常任务：食物、工具、熔炼、装备、床、住所、储物、死亡掉落恢复与维护 | 按新鲜状态重算任务；小批采集、施工最多 8 步且逐步复核。5×5 住所检查只验证结构，不保证照明或完全防怪。 |
| survival / competition | 模型选择得分目标，沿日常动作目录执行，以服务器 advancement 计分 | 125 项规则目录中只有 7 个早期计分目标有明确行动计划，尚非完整赛事执行器。 |

starter、daily 支持规则规划或开启模型规划；competition 要求模型。生存 harness 串行执行技能，模型返回后重新观察并通过本地安全门禁，再按背包、方块或位置变化验证结果。工具调用返回、移动达到容差和实际取得物品是不同证据；离线协议覆盖不等于真实技能成功，更不等于完整生存或比赛完成。

日常记忆使用按世界和账号隔离的 SQLite，记录经验、地点和失败策略；召回只提供候选，执行仍依赖新鲜观察。采种未掉种子不计作取得种子；局部清障只清一格相邻可见树叶/藤蔓，禁止挖自身支撑和头顶。实现与限制见 [日常生存说明](docs/DAILY_SURVIVAL.md)。

### 模型实际可调用的工具

工具目录由 `agent/tools.py` 的 `tools_for_objective()` 选择，执行由 `core/survival_harness.py` 及对应技能分派：starter 为 9 项，daily 为 31 项，competition 为 36 项（31 个日常工具 + 5 个赛事生命周期工具）。模型只能选择当前已注册且可分派的技能。

合并新增的独立技能与主链路关系如下：

| 开发模块 | 可用入口 | 与生存模型的关系 |
| --- | --- | --- |
| `skills/crafting.py` / `CraftItemSkill` | 独立 Python 技能；`ChatDebugger` 注入 `craft_skill` 后提供 `#craft` | 主入口未注入该聊天技能。模型已有的同名 `craft_item` 仍分派到 `SurvivalSkills` / `DailySurvivalSkills`，不是此独立宏技能。 |
| `skills/mining.py` / `ChopTreeSkill` | 独立 Python 技能；注入 `chop_skill` 后提供 `#chop` | 主入口未注入；`chop_tree` 未绑定 starter/daily/competition。 |
| `skills/exploration.py` / `ContinuousRadarSkill` | 主入口启用调试并加 `--radar`；提供 `#radar on/off/scan/poi` | 属于调试雷达与空间记录入口，`radar_sweep` 未绑定生存模型。 |
| `skills/portal_speedrun.py` / `NetherPortalSpeedrunSkill` | 独立 Python 技能与专用实服靶场脚本 | `cast_nether_portal` 未绑定生存模型，也无主入口聊天命令。 |
| `skills/stronghold.py` / 要塞计算 | 独立 Python Skill 与 CLI，schema 为 `STRONGHOLD_TOOL_DEFINITION` | `calculate_stronghold` 未进入上述模型目录。 |

全局工具定义或独立 schema 的扩展不等于 harness 已绑定执行器，必须同时核对目录、参数校验和 dispatch。搭高、落地水及高频反射仍未完成，相关占位实现返回成功不能作为真实执行证据。旧 `agent/loop.py` 的 TODO 不代表当前 `main.py` → `SurvivalHarness` 生存主循环未接通。

### 赛事范围

规则快照 `data/competition/rules_v2.json` 共 125 项（120 个 advancement 目标、5 个根节点），其中 45 项明确计分、54 项条件待确认、26 项不计分。条件项默认不计分，须通过 `survival.competition.eligibility_overrides` 显式确认；只采纳服务器证据，未知不计分，归一化得分暂为 `null`。

Harness 每次只维护一个活动目标。当前仅 7 个明确计分目标有行动计划，其余可计分项虽能评估和展示，但标为 unsupported、不可选择。`finish_task` 需 fresh advancement 检测，正常 `end_competition` 需 fresh 全目录检测；未知可记为 unknown，但不能加分。终态持久化后，重启不再调用模型。离线覆盖不能称为完整比赛验收，详见 [赛事接口与限制](docs/HARNESS_INTERFACES.md)。

## 模型、恢复与监控

模型规划需同时设置 `agent.mode: survival` 与 `llm.enabled: true`，配置支持原生工具调用的 OpenAI 兼容服务。下面是 `config.yaml` 对应字段的节选，请修改已有节点：

```yaml
agent:
  mode: survival
llm:
  enabled: true
  base_url: "https://your-model-provider.example/v1"
  model: "default"  # 替换为服务允许的请求路由
  api_key: ""       # 通过 BOTSLEEP_LLM_API_KEY 提供
  timeout_seconds: 60
debug:
  monitor:
    enabled: true
    host: "127.0.0.1"
    port: 18930
```

所有模型请求经过 `core/model_gate.py`，单 worker 与跨进程门禁禁止并发请求。传输超时或连接中断会留下持久化未知状态，不自动重试、不擅自清锁，也不能靠重启绕过；明确 HTTP 失败才可有界退避，模型错误不回退为规则采集。只有确认服务端请求已结束，才可使用 `--acknowledge-model-idle`。`llm.model` 只是请求路由；实际模型只采用 `response.model`，缺失显示“未确认”，服务自报标识也不证明底层型号。

游戏动作与模型请求有独立恢复流程。动作前持久化 pending 意图，未知写操作结果会暂停；`--resume` 先重新观察再规划，绝不自动重放。检查点与事件按 API、账号和 `survival.world_id` 隔离保存于 `survival.state_dir`；**更换世界必须修改 world_id**，API 不提供存档身份。普通启动及未带 `--resume` 的 `--once` 不绕过 pending。

```powershell
.\.venv\Scripts\python.exe -B main.py --config config.yaml --mode survival --resume
```

步骤、时间、探索和死亡预算依然有效，不承诺无限无人值守。starter 完成退出 0，步骤/时间预算耗尽退出 2；生存 `--once` 只执行一次观察决策，退出 0 也可能只是等待成功，不代表整体目标完成。自动睡眠失败会按配置冷却；单次睡眠无床、白天、中断或超时等返回非零。

启用监控后访问 [本机只读面板](http://127.0.0.1:18930)，可查看 heartbeat、Agent 阶段、任务板、模型请求槽、原始返回、pending 与赛事快照。API 端点延迟、检查点健康度和可靠性告警仍待补。历史按请求字节预算裁剪，不等于语义 compact；阶段 skill 加载、handoff、水面优先导航和自动搭路仍待实现。详见 [监控说明](docs/MONITORING.md)。

## 聊天调试与独立开发入口

先在 `debug.allowed_usernames` 写入可信玩家名，再用 `--debug` 或 `debug.enable_in_game_commands` 启用；空名单拒绝远程命令。支持标准 `<Player> #command` 格式，以 `#help` 显示当前实际注册命令：`#status`、`#inv`、`#stop`、`#sleep`，生存模式另有 `#resume`。`#stop` 请求取消并暂停自动尝试，不保证后端动作瞬时终止。

雷达开发入口需同时启用调试：

```powershell
.\.venv\Scripts\python.exe -B main.py --config config.yaml --debug --radar
```

这会启动雷达扫描并记录空间信息；不是只读离线命令。`#craft` / `#chop` 需要开发者另行注入对应技能，不能仅凭 `--debug` 调用。详见 [调试指南](docs/DEBUG_GUIDE.md) 和 [技能契约](docs/SKILL_SPEC.md)。

要塞计算需要 JDK 11+。以下 setup 下载并校验固定版本 Ninjabrain Bot 1.5.2 JAR，calculate 使用合成数据，不连接游戏：

```powershell
.\.venv\Scripts\python.exe -m integrations.ninjabrain setup
.\.venv\Scripts\python.exe -m integrations.ninjabrain calculate --input data/examples/stronghold_throws.json
```

`capture` 会登录游戏并消耗末影之眼，不属于离线计算。缺少 JAR 时引擎测试显式跳过，不能算完整引擎验证；生成的 `.cache/ninjabrain/` 不提交，上游许可证见 `third_party/ninjabrain/`。详见 [要塞计算与采样](docs/STRONGHOLD.md)。

## 测试

离线回归使用标准库 unittest，模拟 HTTP、模型与 RCON；本机临时 HTTP 假服务不连接真实游戏或模型。完整结果、源码基线、环境差异和历史实服证据见 [TESTING.md](docs/TESTING.md)，最新测试数字由该记录统一维护，历史结果不代表当前合并版本。

```powershell
# 离线回归：保持实服测试未启用
Remove-Item Env:BOTSLEEP_RUN_LIVE_TESTS -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

`tests/test_speedrun_portal.py` 的 RCON 门户测试、`tests/test_random_lava_matrix.py` 的随机岩浆矩阵，以及独立 `tests/test_crafting.py`，属于**实服破坏性专用靶场验证**：会清背包、发物、传送或改方块，必须使用隔离测试世界，不默认运行，不能当作离线单测成功证据。

实服入口要求显式设置 `BOTSLEEP_RUN_LIVE_TESTS=1`，并配置 `MC_AGENT_API`、`MC_AGENT_USER`、`MC_AGENT_PASSWORD`、`MC_RCON_HOST`、`MC_RCON_PORT`、`MC_RCON_PASSWORD`；当前固定靶场账号为 `SleepBot`。未 opt-in 时门户用例跳过，合成与岩浆矩阵脚本拒绝执行；不使用硬编码密码作为默认配置。门禁隔离回归使用禁止网络连接的子进程验证。只核对模型目录时可运行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_merged_tools.py -v
```

RCON 仅供开发靶场，另需安装可选依赖；安装不会授权实服测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 项目入口与文档

| 路径 | 用途 |
| --- | --- |
| `main.py`、`core/config.py` | 配置装配、启动与退出清理。 |
| `core/client.py` | 统一 mc-agent-api HTTP 交互。 |
| `core/survival_harness.py`、`core/survival_state.py` | 串行任务调度、状态观察、pending 与检查点恢复。 |
| `agent/`、`core/model_gate.py` | 规则/模型决策、工具目录、任务图与模型请求门禁。 |
| `skills/` | 睡眠、生存动作与独立开发技能，遵循 `BaseSkill` / `SkillResult`。 |
| `core/competition_rules.py`、`core/competition_tasks.py` | 赛事规则快照、服务器证据计分与生命周期。 |
| `debug/`、`tests/` | 聊天调试、脱敏日志、只读监控与回归测试。 |

- [当前架构](docs/ARCHITECTURE.md)：starter/daily/competition 运行链路与设计边界。
- [生存运行指南](docs/SURVIVAL.md) / [日常任务与记忆](docs/DAILY_SURVIVAL.md)：配置、技能、安全策略与恢复。
- [技能契约](docs/SKILL_SPEC.md) / [调试指南](docs/DEBUG_GUIDE.md)：技能开发与聊天/RCON 入口。
- [赛事接口与未来提案](docs/HARNESS_INTERFACES.md)：已实现的任务子集及尚未绑定的接口。
- [监控](docs/MONITORING.md) / [测试记录](docs/TESTING.md) / [要塞计算](docs/STRONGHOLD.md)：观察、验证与专项集成。

所有适配和修复在 BotSleep 内完成，不修改或提交相邻 `mc-agent-api` 仓库；上游能力不足时记录限制，不依赖本地 API 补丁。

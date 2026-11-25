# BotSleep 交接文档（`develop-tianya/api-model-opt`）

最后更新：2026-11-25（基线提交 `83ca6b0` + 本文档与测试记录更新；本轮按约定在 `nether_bench_v31` 完成后停止继续实服迭代，准备合入 `main`）。

---

## 一、分支状态与相对于 `origin/main` 的核心改动

当前工作分支 `develop-tianya/api-model-opt` 相对 `origin/main` 领先 45+ 提交且 `0` 落后（无冲突，可直接合入或经 PR 合入），主要增量集中在 **稳定性治理、双模型溢出路由、自主子任务鲁棒性与多 Bot 压测链路**，且 **未对相邻 `mc-agent-api` 仓库做任何源码修改**：

1. **双 API 溢出路由与并发门禁**（[agent/brain.py](../agent/brain.py)、[agent/llm_planner.py](../agent/llm_planner.py)、[core/model_gate.py](../core/model_gate.py)、[core/config.py](../core/config.py)）：
   - 默认主路由保留共享并发=1 FIFO 队列（`ModelRequestGate(global_concurrency=True)`），满足单路限流端口约束；
   - 新增可选 `llm.overflow`：当主队列拥塞超过 `primary_wait_seconds`（如 `1.5s`）或主路由返回 HTTP/响应错误进入冷却（`cooldown_seconds: 45`）时，自动按 Bot 隔离 `scope_id` 溢出至支持高并发的兼容端点（如 `https://doro.lat/v1`，请求模型 `deepseek-v4.1-flash`；`competition` 模式默认强制关闭 `overflow`，除非显式设 `allow_in_competition: true`）；
   - 记录实际 `route`（`primary` / `overflow`）、`overflow_reason`、`actual_model`（取自响应 `response.model`）、`elapsed_ms` 与 `usage`，密钥脱敏；支持 `recover_uncertain_transport`（默认 `4..256s` 退避及 `30s` 传输不确定窗口恢复）。
2. **`SafetyArbiter` 分级抢占与短观察升级**（[core/safety_arbiter.py](../core/safety_arbiter.py)、[core/survival_harness.py](../core/survival_harness.py)）：
   - `planning=True` 且启用 `llm_override_on_block` 时，规划 HTTP 等待期间仅在近身极端危险（`<= 2.2m`）、实际掉血/受击或环境危急时抢占，消除远处游荡怪频繁打断 LLM 调用的 `llm_cancelled` 抖动；模型返回后仍用全量新鲜状态严格重检；
   - 连续 `>= 2` 次被动 `wait(1)` 滞留短快照时强制升级为完整观察（`collect()`），防止远离/遮挡敌人长期饿死主任务。
3. **`LoopSupervisor` 双级看门狗 + 各类死循环消除**（[core/loop_supervisor.py](../core/loop_supervisor.py)、[core/retreat_controller.py](../core/retreat_controller.py)、[core/local_recovery_runtime.py](../core/local_recovery_runtime.py)、[core/daily_policy.py](../core/daily_policy.py)）：
   - L1（`action_watchdog_seconds: 30`）：对单步与长工作流（如 `deep_mine`、`scout_surface`、`nether_portal`）基于位置变化 / `activity_seq` / 写操作时间监控超时并在必要时中止；
   - L2：高频 `llm_cancelled` 护盾、静止 `wait` 活锁告警、同签名失败冷却、夜间多死点暂停地面 `recover_drops`；
   - 消除 `inspect_container` 重复检查熔炉循环、短等待清零 `Local footing` 计数循环、`canopy_escape` 因 `near_hostile` 永久锁树冠、低血 `food_recovery_blocked` 锁死及撤退抖动。
4. **`IntentCompiler` 与阶段性进门（`nether_portal`）扩展**（[core/intent_compiler.py](../core/intent_compiler.py)、[core/autonomous_tasks.py](../core/autonomous_tasks.py)、[core/autonomy_runtime.py](../core/autonomy_runtime.py)、[skills/buckets.py](../skills/buckets.py)、[skills/survival.py](../skills/survival.py)、[skills/scouting.py](../skills/scouting.py)、[skills/mining.py](../skills/mining.py)、[skills/ore_harvester.py](../skills/ore_harvester.py)）：
   - 复用已有矿道与开局阶梯、挖净已开矿脉、禁用无镐硬挖岩石与重复造工作台、`4` 铁提示 `PORTAL_IRON_READY`、砂砾采集产燧石 + 优先放置背包砂砾、`water_bucket` 放松至 `health >= 3 / food >= 4`（岩浆仍严守 `health >= 12 / food >= 8`）、`escape_to_surface` 地下垂直安全脱困（遇水/岩浆/坠落砂砾与缺镐即安全停止）；
   - 新增单端口多 Bot 并发启动器 [scripts/multi_bot_runner.py](../scripts/multi_bot_runner.py) 与完整配置参考 [docs/CONFIGURATION.md](CONFIGURATION.md)。

---

## 二、如何在其它电脑运行

### 1. 环境准备

- **操作系统**：Windows 10/11（本分支实测为 Windows 11 + PowerShell 7 / Python 3.13）或 Linux/macOS；建议使用 `Python 3.10+`。
- **Node.js**：`mc-agent-api >= 1.3.13`（当前实测 `1.3.19`）需要 Node.js 18+/20+。
- **Java（可选）**：
  - 仅在本地同时启动 Minecraft 服务端（如 Paper / Leaves 1.21.11，需 JDK 21）或运行 `integrations.ninjabrain` 要塞计算引擎（需 JDK 11+）时安装；若连远程比赛/世界服，本机无需 Java。
- **仓库与依赖**：确保 BotSleep 与官方未修改的 `mc-agent-api` 并排（或通过 `--api-dir` 指定路径）：

```powershell
git clone https://github.com/Mxpea/BotSleep.git
cd BotSleep
git checkout develop-tianya/api-model-opt

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

若本机未运行过 `mc-agent-api`，先进入相邻 `mc-agent-api` 目录执行 `npm install` 并参照其示例配好 `config.json`（目标服务器 IP、端口、离线或 Yggdrasil 验证、代理就绪规则；详见 [CONFIGURATION.md](CONFIGURATION.md)）。

### 2. 配置密钥与校验（严禁把真实 Key 提交进 Git）

编辑被 `.gitignore` 忽略的 `config.yaml`（或优先使用环境变量注入）：

```powershell
# 必填：主模型端点与密钥（若开启 llm.enabled: true）
$env:BOTSLEEP_LLM_API_KEY = "<你的主模型 API Key>"
$env:BOTSLEEP_LLM_BASE_URL = "https://your-primary-endpoint/v1"
$env:BOTSLEEP_LLM_MODEL = "default"

# 可选：高并发溢出路由（日常多 Bot 推荐开启；赛事默认禁用）
$env:BOTSLEEP_LLM_OVERFLOW_API_KEY = "<你的溢出 API Key>"
$env:BOTSLEEP_LLM_OVERFLOW_BASE_URL = "https://doro.lat/v1"
$env:BOTSLEEP_LLM_OVERFLOW_MODEL = "deepseek-v4.1-flash"

# 启动前务必先做离线配置校验（不联网、不连游戏）：
.\.venv\Scripts\python.exe -B main.py --config config.yaml --check-config
```

> [!CAUTION]
> **更换 Minecraft 世界存档或服务器后，必须修改 `survival.world_id`**（或使用新 `--run-id`），否则会读取旧世界坐标与检查点导致状态错位或误判 `pending`。

### 3. 运行方式

**方式 A：单 Bot 运行（无需 RCON）**
1. 启动 `mc-agent-api`（例如在 `mc-agent-api` 目录运行 `node src/index.js config.json`，默认监听 `http://127.0.0.1:18900/api/v1`）。
2. 启动 BotSleep：
```powershell
# 日常生存 / 速通模式（需 config.yaml 设置 agent.mode: survival 与 llm.enabled: true）
.\.venv\Scripts\python.exe -B main.py --config config.yaml --mode survival --objective daily

# 赛场模式（自动强制禁用 overflow，除非 allow_in_competition=true）
.\.venv\Scripts\python.exe -B main.py --config config.yaml --mode survival --objective competition
```
若启用监控（`debug.monitor.enabled: true`），浏览器访问 `http://127.0.0.1:18930` 查看只读面板。

**方式 B：多 Bot（例如 9 Bot）单端口并行压测（可选 `--spread` 分散出生）**
- 单个 `mc-agent-api >= 1.3.13` 进程按 Bot 隔离 Node 子进程（约 220 MB/Bot），共享 `18900` 端口；
- 若使用 `--spread`（开局清包并按至少 `1500` 格距离分散传送），需保证 Minecraft 服务端开启 RCON（`enable-rcon=true`）且模板 YAML 中 `rcon.enabled: true` 与密码配置正确；否则去掉 `--spread` 即可零 RCON 启动：
```powershell
.\.venv\Scripts\python.exe -u scripts\multi_bot_runner.py `
  --config config.yaml `
  --run-id my_bench_run `
  --count 9 `
  --start-api `
  --api-dir ..\mc-agent-api `
  --api-config ..\mc-agent-api\config.json `
  --spread --min-dist 1500
```
各 Bot 配置/状态分别隔离在 `runtime/<run-id>/bot_<i>/`，日志在 `logs/<run-id>/bot_<i>/`，监控端口依次为 `18931..18939`。

**停止与清理进程**
- 正常退出可等预算到期（exit code `0` / `2`）或在前台 `Ctrl+C`；
- 在 Windows 若强制中断脚本，请检查并结束残留子进程以免占用 `18900`/`25565` 端口或锁住 `world`：
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mc-agent-api|multi_bot_runner|main\.py' } | Select-Object ProcessId, Name, CommandLine
```

### 4. 换机验证命令

```powershell
$env:BOTSLEEP_RUN_LIVE_TESTS = '0'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m compileall -q core skills agent main.py bot_sleep.py
.\.venv\Scripts\python.exe -B main.py --config config.example.yaml --check-config
```

---

## 三、Bot 耗时情况与 Token / 额度消耗（实测汇总）

以下数据直接统计自近几轮 **90 分钟 / 9-Bot 分散出生**全量 JSONL 飞行日志（`runtime/live_bench_9bot_5400s.yaml`：`max_tokens=512`，`max_history_turns=2`，`primary_wait_seconds=1.5`，`primary: https://api.nju-aia.com/v1 (qwen3.6-35b-a3b)`，`overflow: https://doro.lat/v1 (deepseek-v4.1-flash -> deepseek-ai/DeepSeek-V4.1-Flash-1017)`）：

### 1. LLM 请求、路由分担、延时与 Token 消耗

| 轮次 | 运行时长 / 状态 | 总请求 / 成功响应 | 主路由 (`qwen3.6-35b-a3b`) | 溢出 (`DeepSeek-V4.1-Flash`) | 总 Token（Prompt / Completion） | 缓存命中 (`cached_tokens`) | 延时 p50 / p95（主 / 溢） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `v25` | 90m / 9/9 跑完 | 2,893 / 2,869 | 648 次 / 6.57M tok | 2,221 次 / 22.00M tok | **28,573,682**（28.29M / 0.28M） | 11.16M (39.4%) | 4.78s / 10.10s（主 7.60s / 溢 4.42s） |
| `v26` | 89m / 9/9 跑完 | 2,469 / 2,369 | 689 次 / 7.20M tok | 1,680 次 / 17.02M tok | **24,222,021**（23.98M / 0.24M） | 8.78M (36.6%) | 4.66s / 8.61s（主 6.75s / 溢 4.22s） |
| `v29` | ~68m（主动中止） | 1,115 / 1,084 | 504 次 / 5.12M tok | 580 次 / 5.75M tok | **10,872,946**（10.75M / 0.12M） | 2.73M (25.4%) | 4.97s / 7.92s（主 5.99s / 溢 4.45s） |
| `v30` | 90m / 9/9 跑完 | 2,384 / 2,368 | 753 次 / 7.67M tok | 1,615 次 / 16.34M tok | **24,016,437**（23.76M / 0.25M） | 8.17M (34.4%) | 4.63s / 8.24s（主 6.52s / 溢 4.24s） |
| `v31` | 90m / 9/9 跑完 | 2,007 / 1,988 | 635 次 / 6.66M tok | 1,353 次 / 13.71M tok | **20,372,954**（20.18M / 0.19M） | 6.59M (32.6%) | 4.78s / 9.58s（主 7.48s / 溢 4.22s） |

- **单 Bot 额度经验值**：在启用自主子任务程序（`autonomy.enabled: true`）后，单 Bot 每小时平均约 **150 万 – 200 万 total tokens**（90 分钟全员约 **2,040 万 – 2,860 万 tokens**，取决于探索/升级重规划次数；例如 `v31` 中反复探索遇阻的 `bot_4` 消耗 `6.66M`、平稳挖矿推进的 `bot_2/bot_9` 仅 `0.43M–0.71M`）。
- **结构特点**：单次请求平均约 **~10.1K prompt tokens + ~96 completion tokens**，因此费用主要来自上下文 Input tokens；双路方案将 **68%–77%** 的并发请求分流到高并发的溢出通道（`DeepSeek-V4.1-Flash`），且 Prompt Prefix Cache 命中率达 **32%–39%**。
- **说明**：代码当前 **未设置硬性 token 配额上限**（按先前确认跳过）。如需控本，可在账单侧加每日限额，或继续压缩 `DAILY_SYSTEM_PROMPT` / `observations` JSON 体量。

### 2. Bot 90 分钟动作与时间分配（`v30` / `v31` 实测汇总，相邻 `STEP_RESULT` 挂钟间隔合计 ~43.6K–46.6K bot-seconds）

| 类别 | `v30` 占比（秒） | `v31` 占比（秒） | 主要构成说明 |
| --- | --- | --- | --- |
| 深矿采掘 (`gather_resource` / `deep_mine`) | 18.5% (8,605s) | 24.1% (10,515s) | 含斜井/鱼骨搜铁煤、护怪退回与返表，单次全矿道耗时较长 |
| 被动/安全/熔炼等待 (`wait`) | 23.5% (10,964s) | 19.9% (8,704s) | 熔炼等待（`10s/次`）、安全复查（`1s`）、夜间/冷却等待、模型抢占让路 |
| LLM 规划决策 (`run_survival_task` 派发) | 17.7% (8,267s) | 17.7% (7,717s) | 等主/溢模型规划目标与部分 `LLM_REPAIR` 校验重问 |
| 基础采木/凿石 (`gather_block` / `step_starter_staircase`) | 10.5% (4,917s) | 11.1% (4,868s) | 砍树清叶、`starter_mining` 下矿台阶 |
| 状态观测 (`observe`) | 8.5% (3,982s) | 7.7% (3,367s) | 世界方块与安全快照读取（含 `observation_changed` 重读） |
| 移动/撤退 (`move_to` / `retreat_from` / `position_for_task`) | 11.0% (5,126s) | 10.3% (4,507s) | 地表赶路、宽域找水/矿、夜间遇怪撤退 |
| 合成与装备 (`craft_item` 等) | 3.5% (1,623s) | 3.8% (1,672s) | 含贴近工作台与背包复验 |

- **按目标划分墙钟 Top 5（`v31`）**：`deep_mine` **23.6%**、`safety` **15.7%**、`observe` **11.3%**、`gather` **10.9%**、`scout` **4.8%**、`smelt` **4.3%**。

---

## 四、现在还存在的问题（截至 `v31` 收官实测）

`v24` → `v31` 已彻底解决模型永久卡死、树冠 `near_hostile` 僵死、低血水桶被拒、熔炉重复检查、`gravel` 不可采等 20+ 阻断；到 **`v31`** 已有 **`8/9` 烧铁、`7/9` 铁镐、`8` 个 Bot 进入过 `nether_portal`、`bot_3` 拿齐水桶 + 背包砂砾现场造出打火石并调用 `build_portal`（`8` 次）**。仍存在以下 **5 个未解决实服阻断**（导致本轮仍 `0/9` 真正进入 `the_nether`）：

1. **`_run_task_nether_portal` 找到 32–64m 远岩浆池后仍用局部 32m `move_to` 导致失败**（最直接临门一脚）：
   - 现场证据：`v31` 中 `bot_3` 备齐 `water_bucket + flint_and_steel` 后在 `step 1435..1442` 连续 8 次调用 `move_to(goal="nether_portal")` 全部失败 `out_of_range: Only local movement within 32 blocks is supported`。
   - 原因：[core/autonomy_runtime.py](../core/autonomy_runtime.py) 的 `_run_task_nether_portal` 用 `find_blocks(types=["lava"], radius=64)` 找到熔岩后，直接调用了上限 32 格的 `SurvivalSkills.execute("move_to", ...)`，没有像 `_run_task_wide_find` 那样调用 `_move_wide_candidate(..., allow_partial=True)` 分段逼近。
2. **岸边高低差取水被脚边地面块遮挡射线（`target_occluded`）**：
   - 现场证据：`v31` 中 `bot_1`（`step 172..194`、`water=1/12`）与 `bot_6`（`step 174..182`、`water=0/3`）站在 `y=60` 水岸取 `y=58` 的水，射线在脚前泥土/草方块边缘相交触发 `Official bucket aim ray is obstructed`。
   - 原因：[core/autonomous_tasks.py](../core/autonomous_tasks.py) 的 `_water_shore_stand()` 仅校验水平距离与 `|dy| <= 1.5`，未提前用 `_ray_cells(eye, water_aim)` 预检瞄准射线是否通畅。
3. **跨河流/海洋时地表侦察落水与高频率夜间死亡**：
   - 现场证据：`v31` 的 `bot_3` 拿到 `flint_and_steel` 失败 `out_of_range` 后转而 `scout`，随后反复滑入水中（`step 1447..1462` 触发 5 次 `water_escape`、`pathfinding timed out`）；全员 `v31` 中 `bot_9` 因夜间复活点附近反复死亡触及 `death_limit` 暂停（`64` 死），`bot_3/bot_5/bot_8` 也各自死亡 `7` 次，反复掉落前期装备重头来过。
4. **洞穴深处失镐且工作台在另一高度层时无法 `escape_to_surface`**：
   - 当 Bot 在洞穴里耗尽镐子、而唯一已知 `crafting_table` 停留在深矿层（如 `y=27`）时，`escape_to_surface` 因头顶是石头且无镐安全返回 `missing_pickaxe`，需要上层 `AutonomousTaskController` / `repair` 兜底（就地合成新工作台或石镐，而不是去 20m 垂直落差外找旧工作台）。
5. **高开销长尾（`deep_mine` 与远距无树出生重规划）**：
   - `bot_4` 等开局靠近海洋/无树区域或在 `y=60` 浅层不断重规划 `scout -> gather`，单 Bot 在 `v31` 消耗高达 `6.66M tokens`（含 `120` 次 `LLM_ERROR`、`234` 次 `LLM_REPAIR`）却仍未到铁器门。

---

## 五、逻辑漏洞与边界审计报告

对核心状态机、模型门禁、本地安全、行动技能与多 Bot 编排代码（`agent/brain.py`、`agent/llm_planner.py`、`core/model_gate.py`、`core/safety_arbiter.py`、`core/loop_supervisor.py`、`core/retreat_controller.py`、`core/local_recovery_runtime.py`、`core/autonomous_tasks.py`、`core/autonomy_runtime.py`、`skills/buckets.py`、`skills/survival.py`、`scripts/multi_bot_runner.py`）复核结果：

1. **无限死循环与永久锁死风险（已基本封闭，未见未有界自旋路径）**：
   - `LoopSupervisor` 对同一参数失败签名、静止等待、密集 `llm_cancelled` 均设定时间窗口或冷却阈值（`300s`），`RetreatController` 连续 2 轮无新路退出，`LocalRecoveryRuntime`（含最新合入的 `canopy_escape` `near_hostile` 释放）在 3–4 次受阻后退出；
   - **需要注意的剩余循环面**：当模型不断在两个略微不同的工具/坐标（例如 `run_survival_task(gather logs)` 与 `run_survival_task(scout)`）之间交替且每次都有微小位移或 `scout` 仅扫描原地返回 `True`（如 `bot_4` 在海岸附近）时，虽不会在单签名上卡死，但会产生高 token 浪费的“跨目标大环摆”（Macro-oscillation）。
2. **`recover_uncertain_transport` 与主线路超时的语义边界**：
   - [core/model_gate.py](../core/model_gate.py) 的全局锁在 `auto_recover_uncertain=True` 时，允许 `llm_timeout` / `llm_network_error` 在 `TRANSPORT_WINDOW_SECONDS=30s` 后解锁并发起带退避的重新规划；这在日常多 Bot 长时间无人值守压测中消除了全军永久锁死，但在严格禁止任何未知超发计费/重放的 **正式赛事（`competition`）场景，仍建议将 `llm.recover_uncertain_transport` 与 `llm.overflow.enabled` 均保持关闭**，由人工或 `--acknowledge-model-idle` 介入。
3. **主路由传输超时的单次溢出行为**（[agent/brain.py](../agent/brain.py) 第 `212-229` 行）：
   - 当主路由返回 HTTP 4xx/5xx 或损坏 JSON 时，当次调用立刻冷却主路由 45s 并**直接无缝切换**到 `overflow_brain` 返回结果；
   - 当主路由发生请求超时或断网（`llm_timeout` / `llm_network_error`）时，仅先对主路由记 45s 冷却并将异常抛回上层（不自动重放同一次可能已在网关执行的调用），由上层退避 4s 后的下一次独立 `choose()` 再经 `overflow_brain` 发出——这是刻意的幂等安全设计，并非漏写。
4. **`escape_to_surface` 安全前提**（[skills/survival.py](../skills/survival.py) 第 `818-992` 行）：
   - 仅限主世界且不在液体中时运行；每上升 1 格前核查 `[cx, cy+2, cz]`、四向水平邻格液体（防破入海底/岩浆湖）及 `[cx, cy+3, cz]` 顶层坠落块（`sand` / `gravel`）与液体，无镐不挖岩石，遇 `action_unconfirmed` 立即停止并向上保留未知写态；**无绕过或盲目清理逻辑**。
5. **多 Bot 脚本的外部进程生命周期**（[scripts/multi_bot_runner.py](../scripts/multi_bot_runner.py)）：
   - 脚本本身正常退出时会 `terminate()` 其拉起的 `mc-agent-api` 与各 Bot 子进程；但若调用方强杀外层 PowerShell/终端会话，Node 子进程或独立启动的 `server.jar` 仍可能在 Windows 后台滞留并锁定存档。

---

## 六、合入 `main` 准备核查清单

- `git merge-tree --write-tree origin/main HEAD` 已确认与 `origin/main` **无任何合并冲突**；
- `mc-agent-api` 工作区干净未修改；
- 所有实服配置文件、API Key 与 `runtime/` / `logs/` 均未进入版本库（`config.example.yaml` 仅保留占位与 `overflow.enabled: false`）；
- `docs/TESTING.md` 已同步本轮 `v24`–`v31` 实测结果与最新离线全量测试结果（见文档顶部）。

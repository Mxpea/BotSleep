# BotSleep v2 8 小时模拟赛部署与开发成果说明（单 Bot + 仅主 API）

> **分支基线**：`developer-tianya/competition-task-advancements`（基于 `main` @ `7feb2e2`，最新代码基线 `150d2de`；**当前未合入 `main`**，供今日 8 小时模拟赛独立审阅与跨机部署使用）。  
> **运行约束**：相邻 `mc-agent-api`（`1.3.19`）保持**官方原版零源码改动**；比赛模式严格使用 **单 Bot + 仅主路 LLM API**（`llm.overflow.enabled: false`、`llm.overflow.allow_in_competition: false`）。

---

## 一、开发成果速览（Review / 赛前一眼看清）

在 BotSleep v2（L0–L4 单向五层行为树微内核）基础上，本分支完成了**非串行、任务驱动型赛事推进架构**的首次完整落地：

| 模块 / 层级 | 核心新增与改动 | 已解决的赛场痛点 |
|---|---|---|
| **赛事任务图（L3）** | [v2/tasks/competition/graph.py](../v2/tasks/competition/graph.py) | 声明式 `AdvancementNodeSpec`（覆盖 40+ 成就节点的 `handler`、物品前置、`capabilities` 依赖/解锁、世界触发词、基准耗时与单任务止损超时） |
| **非串行调度器（L4）** | [v2/planner/competition_scheduler.py](../v2/planner/competition_scheduler.py)、[v2/planner/horizon_selector.py](../v2/planner/horizon_selector.py)、[skills_kb/competition_scheduler.md](../skills_kb/competition_scheduler.md) | 按 **PPM（分/分钟）+ UnlockSynergy（解锁连携）+ TimeFit（残局时间窗口匹配）+ 24m 机会命中加成 + 失败指数退避**动态择优，替代固定线性主线 |
| **6 类可复用成就插件（L3）** | [v2/tasks/advancement_actions.py](../v2/tasks/advancement_actions.py)、[v2/tasks/mob_gather.py](../v2/tasks/mob_gather.py)、[v2/tasks/catalogue.py](../v2/tasks/catalogue.py) | 统一封装交互方块/实体、使用物品与钓鱼、远程/战斗、容器窗口操作（对齐 `mc-agent-api` 字段）、结构搭建及生物掉落采集；优化 `obtain_armor`（`iron_boots` 仅需 4 铁）等 `ADVANCEMENT_GOAL_ROUTES` |
| **零额外请求侦察与止损（L2/App）** | [v2/app.py](../v2/app.py)（`IslandReconIndex`、`CompetitionSupervisor`）、[v2/bt/branches/hard_safety.py](../v2/bt/branches/hard_safety.py) | 被动沉淀周边环境侦察；按 `timeout_seconds` 及时止损换线并输出 `COMPETITION_SELECT` / `COMPETITION_TIMEOUT` / `COMPETITION_SCORE`；服务端 `/advancements` 一旦确认拿分立即卸载当前目标；新增悬崖落空防护 |
| **本地排障与复盘工作台** | [scripts/debug_workbench.py](../scripts/debug_workbench.py)、[scripts/botctl.py](../scripts/botctl.py) | 支持赛后离线生成赛事排行榜、PPM、Top-3/Bottom-3 对比与卡死热点报告；支持双端 `BotSleepDebug` 对齐 |

### 1.1 已验证数据（严格区分离线单测与实服运行）

- **离线单元测试（纯内存仿真，不连真服/网络）**：  
  在提交 `150d2de` 上执行 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2 -v` 通过 **`184 / 184`**（`111.386s`，`0` 失败、`0` 错误、`0` 跳过），包含新增的 [tests_v2/test_29_competition_dag_and_actions.py](../tests_v2/test_29_competition_dag_and_actions.py)。
- **实服 3 轮 9-Bot 对照赛（Leaves `1.21.11`、`BotSleepDebug v1.0.0`、未修改 `mc-agent-api 1.3.19`，以服务端 `/advancements` 45 项无条件成就为唯一计分真值）**：

| 轮次 | 代码基线 | 单机时长 | 退出状态 | 平均分 / 最高分 | 关键进展与根因收敛 |
|---|---|---|---|---|---|
| **Comp1** (`v2_live9_comp1_120m`) | `0f54f29` | 120 分钟 | `9/9 rc=0` | `25.0` / `35`（3 名 `>=30`） | 验证跑分链路；`smelt_iron` `3/9`、`plant_seed` `8/9`、`kill_a_mob` `8/9`、`obtain_armor` `2/9`、`fishy_business` `1/9`、`shoot_arrow` `1/9`、`sleep_in_bed` `1/9`、`breed_an_animal` `1/9`；暴露 `NEEDS_AI` 退避与床接近卡死 |
| **Comp2** (`v2_live9_comp2_120m`) | `35b845b` | 120 分钟 | `9/9 rc=0` | `18.89` / `30` | 死亡降至 `59`（`-29.8%`）、床锁死 `0`、`smelt_iron` `3/9`、`iron_tools` `2/9`；精确定位地下断镐锁死（`1,036` 次 / `19,155` ticks）与主世界误选下界任务（`6,908` ticks） |
| **Comp3** (`v2_live9_comp3_60m`) | `150d2de` | **60 分钟**（半时长） | `9/9 rc=0` | **`26.11`** / `35`（**4 名 `30–35`**） | **时间减半反而均分最高**；`smelt_iron` **`8/9`（88.9%）**、`iron_tools` **`4/9`（44.4%）**；`unstuck_l4_exhausted` 卡死拍数 **`19,155 → 91`（`-99.5%`）**、`UNDERGROUND_NO_PICK_LOCK` **`1,036 → 4`**、`bare_hand_rock` **`108 → 6`**、主世界误选下界任务 **`0`** |

### 1.2 当前未完成项与已知边界（诚实交付说明）

1. **8 小时单 Bot 完整连续实服尚未跑过**：目前最长实服连续运行为 `120` 分钟，8 小时赛程属于长时稳定性首跑验证。
2. **下界与末地成就尚未在实服短跑中拿到计分**：相关任务节点与本地传送门逻辑已存在并通过离线测试，但 `60–120` 分钟实服压测尚未观察到 Bot 进门拿分。
3. **54 项条件草案成就默认关闭**：仅 `rules_v2.json` 中的 45 项无条件成就默认启用；若今日赛方最终规则明确计入某些条件项，需手动写入 `survival.competition.eligibility_overrides`。
4. **已知边缘小缺陷（不影响主干跑分，但赛前需知晓）**：
   - `sleep_in_bed` 当前依赖 3 个羊毛合成路径，暂未自动凑 4 线合成羊毛兜底；
   - `breed_an_animal` 暂未补充「猪 + 胡萝卜」繁殖路线（优先牛/羊小麦、鸡种子）；
   - 复杂地下洞穴或夜战围攻下仍可能出现局部死亡或短时徘徊（已设单任务 `timeout_seconds` 自动止损跳出）。

---

## 二、新电脑零基础部署指南（单 Bot + 仅主 API + 8 小时模拟赛）

以下步骤在**全新电脑（Windows PowerShell 或 Linux / macOS Bash）**上从零可执行，全程不依赖多机脚本，只启用主 LLM API。

### 1. 准备前置环境

- **Git**、**Python `3.10 – 3.13`**
- **Node.js（推荐 18+ / 20+）**：仅当本机需要同时启动相邻 `mc-agent-api`（`1.3.19`）时使用
- 赛方提供信息：Minecraft `1.21.11` 服务器地址/端口、认证方式（离线或 Yggdrasil 外置皮肤站账号密码）、**主 LLM API** 的 `base_url`、`api_key` 与允许的 `model` 路由名

### 2. 拉取本分支代码并安装隔离依赖

Windows（PowerShell）：

```powershell
git clone -b developer-tianya/competition-task-advancements https://github.com/Mxpea/BotSleep.git
Set-Location BotSleep
$env:PYTHONUTF8="1"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux / macOS（Bash）：

```bash
git clone -b developer-tianya/competition-task-advancements https://github.com/Mxpea/BotSleep.git
cd BotSleep
export PYTHONUTF8=1
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

> 若新电脑已有旧克隆目录，请执行 `git fetch origin && git checkout developer-tianya/competition-task-advancements && git pull`。注意本部署**不要切换或合并到 `main`**。

### 3. 启动未修改的原版 `mc-agent-api`（若网关不在远端）

在与 `BotSleep` 相邻或独立目录部署原版 `mc-agent-api`（`1.3.19`，不要修改其源码），将 `config.json` 指向比赛服（离线/外置认证及 Velocity 子服 `readiness` 写法详见 [CONFIGURATION.md](CONFIGURATION.md#二配置-mc-agent-api-对接目标服务器)），然后启动使其监听 `http://127.0.0.1:18900/api/v1`。

### 4. 创建单 Bot + 仅主 API 的 8 小时比赛配置（`config.yaml`）

在 `BotSleep` 根目录新建 `config.yaml`（**已被 `.gitignore` 忽略，禁止把真实密钥/密码提交到 Git**），直接复制以下模板并替换 `<...>` 占位符：

```yaml
mc_api:
  base_url: "http://127.0.0.1:18900/api/v1"
  timeout_seconds: 15
  login_timeout_seconds: 45        # 外置登录或群组服切服建议 45~60s
  keep_session_on_exit: false

agent:
  name: "<Bot角色名>"              # 3~16位字母/数字/下划线，或 Yggdrasil 角色名
  password: ""                     # 建议通过环境变量 MC_AGENT_PASSWORD 注入
  mode: "survival"

survival:
  objective: "competition"         # 正式/模拟赛模式
  intent: "survival"
  sleep_policy: "opportunistic"
  disable_sleep: false             # 单 Bot 赛服支持直接找床睡觉拿 sleep_in_bed 分并跳过黑夜
  world_id: "mock_match_20261128"  # 【关键】每次更换/重置比赛世界必须换新名字！
  state_dir: "runtime/mock_match_20261128"
  poll_interval_seconds: 1
  max_deaths: 50
  autonomy:
    enabled: true
    local_opening: false
    max_steps: 128
    max_seconds: 600
    max_local_failures: 4
    max_no_progress_steps: 8
    max_explore_steps: 8
  competition:
    eligibility_overrides: {}      # 仅当赛方最终确认计入特定条件项时再填入 {"minecraft:...": true}
    defer_seconds: 300
    advancement_poll_seconds: 5
    max_seconds: 28800             # 精确 8 小时 = 28800 秒
    max_steps: 150000              # 8 小时充足步数预算（competition 支持最高 1,000,000）

debug:
  enable_in_game_commands: false   # competition 模式必须为 false
  flight_recorder:
    enabled: true
    output_dir: "logs/mock_match_20261128"
  monitor:
    enabled: true                  # 仅本机只读状态面板 http://127.0.0.1:18930
    host: "127.0.0.1"
    port: 18930
    heartbeat_seconds: 2
    console: false

llm:
  enabled: true                    # competition 模式必须为 true
  base_url: "https://<主API地址>/v1"
  api_key: ""                      # 建议通过环境变量 BOTSLEEP_LLM_API_KEY 注入
  model: "<主API允许的模型路由名>"
  temperature: 0.3
  max_tokens: 2048
  timeout_seconds: 60              # 禁止低于 60s，防止高上下文被过早判定超时
  max_history_turns: 8
  max_repair_attempts: 1
  tls_version: "auto"              # 若代理报 TLS 握手错可改为 "1.2"
  gate_wait_seconds: 600
  recover_uncertain_transport: true
  retry_backoff_seconds: [4, 16, 32, 64, 128, 256]
  overflow:
    enabled: false                 # 本次只用主 API：显式关闭溢出双路
    allow_in_competition: false
```

### 5. 环境变量注入、清理残留覆盖与配置校验

在新电脑的当前终端注入主 API 密钥与 Bot 密码，并检查/清理可能误覆盖 YAML 的旧环境变量（尤其检查不要残留 `BOTSLEEP_LLM_OVERFLOW_*`）：

Windows（PowerShell）：

```powershell
# 1) 检查并清理可能残留的环境变量（如有输出且非当前赛场配置请 Remove-Item Env:<名>）
Get-ChildItem Env: | Where-Object { $_.Name -match '^(BOTSLEEP_|MC_AGENT_)' }
Remove-Item Env:BOTSLEEP_LLM_OVERFLOW_ENABLED, Env:BOTSLEEP_LLM_OVERFLOW_API_KEY, Env:BOTSLEEP_LLM_OVERFLOW_BASE_URL, Env:BOTSLEEP_LLM_OVERFLOW_MODEL -ErrorAction SilentlyContinue

# 2) 注入本场凭据（若未写在 config.yaml 中）
$env:MC_AGENT_PASSWORD="<你的Bot登录口令或密码>"
$env:BOTSLEEP_LLM_API_KEY="<你的主API_KEY>"

# 3) 校验配置与全新开赛参数（不连接游戏/大模型；均应输出 Configuration valid.）
.\.venv\Scripts\python.exe -B main.py --config config.yaml --check-config
.\.venv\Scripts\python.exe -B main.py --config config.yaml --mode survival --objective competition --new-competition --check-config
```

Linux / macOS（Bash）：

```bash
env | grep -E '^(BOTSLEEP_|MC_AGENT_)' || true
unset BOTSLEEP_LLM_OVERFLOW_ENABLED BOTSLEEP_LLM_OVERFLOW_API_KEY BOTSLEEP_LLM_OVERFLOW_BASE_URL BOTSLEEP_LLM_OVERFLOW_MODEL

export MC_AGENT_PASSWORD="<你的Bot登录口令或密码>"
export BOTSLEEP_LLM_API_KEY="<你的主API_KEY>"

./.venv/bin/python -B main.py --config config.yaml --check-config
./.venv/bin/python -B main.py --config config.yaml --mode survival --objective competition --new-competition --check-config
```

可选离线全量回归自检（不联网、耗时约 1.5–2 分钟，确认新机器 Python 环境无异常）：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2 -v
```

### 6. 启动 8 小时比赛、途中监控与断线安全恢复

> [!IMPORTANT]
> **切勿在启动 8 小时比赛的命令行上传递 `--max-steps` 参数！**  
> `main.py` 命令行参数 `--max-steps` 硬限制在 `1..10000` 并会直接覆盖 `config.yaml` 中的 `survival.competition.max_steps: 150000`。请省略 `--max-steps`，直接读取 YAML 里的 `28800` 秒 / `150000` 步预算。

以下命令将 `<PY>` 替换为 `.\.venv\Scripts\python.exe`（Windows）或 `./.venv/bin/python`（Linux/macOS）：

```powershell
# A. 全新开赛启动（清空旧赛局计时并作为新场次开启）
<PY> -B main.py --config config.yaml --mode survival --objective competition --new-competition

# B. 只读巡检本地锁、pending 状态与模型门禁（可在另一个终端随时运行，零网络副作用）
<PY> -B scripts/botctl.py --config config.yaml status

# C. 途中若意外掉线 / 进程中断后的安全续跑（严禁与 --new-competition 同时使用！不会重放 unknown 写入）
<PY> -B main.py --config config.yaml --mode survival --objective competition --resume

# D. 仅当主 API 超时挂起、且你在上游面板已确认服务端再无残留请求时，解锁恢复：
<PY> -B main.py --config config.yaml --mode survival --objective competition --resume --acknowledge-model-idle

# E. 赛后离线生成本地复盘报表与卡死热点分析：
<PY> -B scripts/debug_workbench.py logs/mock_match_20261128 --md-out logs/mock_match_20261128/report.md --html-out logs/mock_match_20261128/report.html
```

- 启动后也可在本机浏览器打开只读面板 `http://127.0.0.1:18930` 观察心跳、当前 `active_goal` 与服务端计分。

---

## 三、赛场常见故障速查

| 现象 / 报错 | 原因 | 处置方式 |
|---|---|---|
| 新世界刚开局就跳过很多点或沿用旧位置黑名单 | 忘记修改 `survival.world_id` | 停止后修改 `survival.world_id` 与 `state_dir` 为新标识，并用 `--new-competition` 重启 |
| 跑没多久提示步数预算用完退出 | 命令行误加了 `--max-steps`（上限被截到 `10000`） | 去掉命令行 `--max-steps`，由 `config.yaml` 中 `survival.competition.max_steps` 控制 |
| 提示 `pending_unknown` 冻结拒绝继续 | 上一次写操作在 HTTP 超时后结果未知，安全内核拒绝自动重放 | 先运行 `botctl.py --config config.yaml status` 查看；确认无误后带 `--resume` 续跑（必要时加 `--acknowledge-model-idle`） |
| 报 `SSLError` / TLS 握手失败 | 主 API 所在代理网关对高版本 TLS 协商兼容不佳 | 将 `config.yaml` 中 `llm.tls_version` 从 `"auto"` 改为 `"1.2"`（保持证书校验） |
| 报 `--new-competition cannot be combined with --resume` | 同时传了开新赛与续跑参数 | 首次开赛只用 `--new-competition`；中途掉线重连只用 `--resume` |

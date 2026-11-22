# BotSleep 配置与赛事参数指南

更新：2026-11-22。本指南说明如何配置 Bot 接入目标 Minecraft 服务器、各模式（尤其是比赛模式 `competition` 与日常模式 `daily`）的默认/推荐配置，以及 [core/config.py](../core/config.py) 中全部可调参数的含义与约束。

> [!IMPORTANT]
> 代码内置默认值（`agent.mode: sleep`、`survival.objective: starter`、`llm.enabled: false`）**仅面向最小自动睡眠与基础测试**。若要跑完整生存或参加比赛，必须按下方推荐配置显式启用 `survival`、`daily` / `competition` 与 `llm.enabled: true`。

---

## 一、配置生效优先级与整体结构

一次完整运行涉及两层配置：
1. **`mc-agent-api` 配置（JSON）**：决定网关如何连上目标 Minecraft 服务器（服务器地址、版本、离线或 Yggdrasil 外置认证、代理切服就绪条件）。
2. **`BotSleep` 配置（YAML）**：决定机器人如何连上 API、调用哪家大模型、采用何种生存/比赛策略与安全阈值。

BotSleep 读取配置的严格优先级（后者被前者覆盖）：
1. `--username`（命令行参数，覆盖 `agent.name`） / `--mode` / `--objective`
2. 环境变量：`MC_AGENT_API`、`MC_AGENT_USER`、`MC_AGENT_PASSWORD`、`BOTSLEEP_LLM_API_KEY`、`BOTSLEEP_LLM_BASE_URL`、`BOTSLEEP_LLM_MODEL`
3. 配置文件：`--config <路径>` > 环境变量 `BOTSLEEP_CONFIG` > 仓库根目录 `config.yaml`
4. 代码默认字典：[core/config.py](../core/config.py) 中的 `DEFAULTS`

> 真实密码、API Key、RCON 凭据禁止提交进 Git 仓库；`config.yaml` 与 `.env` 默认已被忽略。相对路径（如 `logs`、`runtime/survival`、`session_file`）均**相对 YAML 配置文件所在目录**解析，而非命令行当前工作目录。

---

## 二、配置 `mc-agent-api` 对接目标服务器

修改 `mc-agent-api` 目录下的 `config.json`（参考 [examples/config.yggdrasil-proxy.json](../../mc-agent-api/examples/config.yggdrasil-proxy.json) 与 [docs/ADAPTERS.md](../../mc-agent-api/docs/ADAPTERS.md)）：

```json
{
  "http": { "host": "127.0.0.1", "port": 18900 },
  "minecraft": { "host": "<比赛服IP或域名>", "port": 25565, "version": "1.21.11" },
  "session": { "tokenTtlHours": 24 },
  "auth": {
    "type": "yggdrasil",
    "authServer": "https://<皮肤站>/api/yggdrasil/authserver",
    "sessionServer": "https://<皮肤站>/api/yggdrasil/sessionserver"
  },
  "readiness": {
    "type": "proxy",
    "server": "<比赛后端子服名>",
    "minAdvancements": 20
  }
}
```

- **`auth`**：
  - 离线服：`{"type": "offline", "password": "<共享API口令>"}`（可选 `"allowedUsernames"` 白名单）。
  - 外置皮肤站（Yggdrasil）：配好 `authServer` 与 `sessionServer`；BotSleep 的 `agent.name` 与 `agent.password` 即填皮肤站角色名/账号与密码。
- **`readiness`**：
  - 直连单服：`{"type": "direct"}`（进入世界即就绪）。
  - Velocity / BungeeCord 群组服：`{"type": "proxy", "server": "<子服名>", "minAdvancements": 20}`（等待切服及服务端进度下发完毕才放行动作）。
- **`http`**：默认监听 `127.0.0.1:18900`。`mc-agent-api >= 1.3.13`（当前基线 `1.3.19`）在单个进程内按子进程隔离多个 Bot，无需再开 `18901..18909` 一排端口。

---

## 三、默认运行与推荐配置模板

### 1. 代码默认值 vs 实际使用选择

| 场景 | `agent.mode` | `survival.objective` | `llm.enabled` | 说明 |
| --- | --- | --- | --- | --- |
| 默认最小自动睡眠 | `sleep` | — | `false` | 不连大模型，仅找床睡觉 |
| 基础石器调试（不推荐长跑） | `survival` | `starter` | 可选 | 仅木/石器/熔炉，无 `AutonomousTaskController` |
| **日常长线生存（推荐测试）** | **`survival`** | **`daily`** | **`true`** | 13 项日常任务 + 12 类自主子循环 + 侦察 + 夜间防卡死 |
| **正式赛事计分（打比赛用）** | **`survival`** | **`competition`** | **`true`** | 基于服务端 advancement 计分，含 40 项赛事工具与生命周期管理 |

### 2. 比赛模式推荐配置（直接复制修改 `<占位符>` 使用）

```yaml
mc_api:
  base_url: "http://127.0.0.1:18900/api/v1"
  timeout_seconds: 15
  login_timeout_seconds: 45        # 公网服或外置登录建议 45~60s，避免登录超时
  keep_session_on_exit: false      # 比赛默认退出登出

agent:
  name: "<Bot角色名或Yggdrasil账号>"  # 3-16位字母/数字/下划线
  password: "<账号密码或API口令>"
  mode: "survival"

survival:
  objective: "competition"         # 比赛必选 competition；日常测试改 daily
  intent: "survival"               # 可选 speedrun：仅影响阶段建议文本，不解锁未绑定工具
  sleep_policy: "opportunistic"    # 强烈推荐：有近床才睡，无床黑夜/雷暴照常干活
  world_id: "match_round_1"        # 换场次/清档重置世界时务必改名！
  state_dir: "runtime/match_round_1"
  operator_goal: ""                # 可选战术提示（<=1000可见字符）
  autonomy:
    enabled: true                  # 推荐保持 true：模型下发高层目标，减少请求数与延迟
    local_opening: false
    max_steps: 128
    max_seconds: 600
    max_local_failures: 4
    max_no_progress_steps: 8
    max_explore_steps: 8
  competition:
    eligibility_overrides: {}      # 如官方放行条件项，填 {"minecraft:story/follow_ender_eye": true}
    defer_seconds: 300
    advancement_poll_seconds: 5
    max_seconds: 28800
    max_steps: 100000
  poll_interval_seconds: 1         # 希望节拍更快可改 0.5
  max_deaths: 5

llm:
  enabled: true
  base_url: "https://<模型API地址>/v1"
  api_key: "<API_KEY>"             # 也可用环境变量 BOTSLEEP_LLM_API_KEY
  model: "<模型路由名>"
  temperature: 0.3
  max_tokens: 2048
  timeout_seconds: 60              # 请勿低于 60，避免端侧高上下文超时锁死
  max_history_turns: 8             # 想压缩上下文/加快响应可设 6~8（默认10）
  max_repair_attempts: 1
  tls_version: "1.2"               # 若代理/端侧网关 TLS 握手报错用 "1.2"，否则 "auto"

debug:
  enable_in_game_commands: false   # competition 模式下禁用游戏内聊天调试
  flight_recorder:
    enabled: true
    output_dir: "logs/match_round_1"
  monitor:
    enabled: true                  # 仅本机只读面板：http://127.0.0.1:18930
    host: "127.0.0.1"
    port: 18930
    heartbeat_seconds: 2
    console: false
```

校验并启动：
```powershell
.\.venv\Scripts\python.exe main.py --config match_config.yaml --check-config
.\.venv\Scripts\python.exe main.py --config match_config.yaml --mode survival --objective competition
```

### 3. 单机多开 Bot 比赛启动

若同一台机器跑多个 Bot，**全部共用一个 `mc-agent-api` 端口（`18900`）**，通过 [scripts/multi_bot_runner.py](../scripts/multi_bot_runner.py) 自动按索引隔离 `state_dir`、`world_id`、`session_file`、日志目录及监控端口（`18931+`）：
```powershell
# 先预览生成的每 Bot 配置（不联网）
.\.venv\Scripts\python.exe scripts\multi_bot_runner.py --config match_config.yaml --count 4 --bot-prefix TeamBot --dry-run

# 确认无误后正式启动（支持 --start-api 一并拉起 API、--login-delay 错峰防 429）
.\.venv\Scripts\python.exe scripts\multi_bot_runner.py --config match_config.yaml --count 4 --bot-prefix TeamBot --login-delay 1.5 --launch-delay 2.0
```

---

## 四、全量参数含义与合法区间（对照 `core/config.py`）

### 1. `mc_api`（API 网关连接）

| 键 | 默认值 | 校验范围 / 类型 | 含义与调优说明 |
| --- | --- | --- | --- |
| `base_url` | `"http://127.0.0.1:18900/api/v1"` | HTTP(S) URL | `mc-agent-api` 接口基址（不可带用户名密码、query 或 fragment） |
| `timeout_seconds` | `15` | 正数 | 常规动作/读取 HTTP 请求超时（秒） |
| `login_timeout_seconds` | `30` | 正数 | `/auth/login` 登录与代理切服等待超时；跨网或切服慢时调高到 `45~60` |
| `keep_session_on_exit` | `false` | 布尔 | 退出时是否保留玩家在线及缓存会话令牌（也可用 CLI `--keep-connected`） |
| `session_file` | `""` | 文件路径 | 自定义会话令牌缓存文件路径；留空时由默认规则处理 |

### 2. `agent`（账号与运行模式）

| 键 | 默认值 | 校验范围 / 类型 | 含义与调优说明 |
| --- | --- | --- | --- |
| `name` | `"SleepBot"` | `[A-Za-z0-9_]{3,16}` | 机器人游戏名；3–16 位 ASCII 字母、数字或下划线 |
| `password` | `""` | 字符串（可空） | 离线口令或外置登录密码；生产环境建议用 `MC_AGENT_PASSWORD` |
| `mode` | `"sleep"` | `"sleep"` \| `"survival"` | `"sleep"` 自动睡眠入口；生存与比赛必须设 `"survival"` |

> 注：旧配置里可能残留 `reflex_enabled`、`reflex_frequency_hz`、`threat_distance_threshold`，配置校验器仍允许其存在以兼容旧文件，但当前版本不会据此开启 20Hz 旧高频反射占位逻辑。

### 3. `survival`（核心生存与安全门禁）

| 键 | 默认值 | 校验范围 / 类型 | 含义与调优说明 |
| --- | --- | --- | --- |
| `objective` | `"starter"` | `"starter"` \| `"daily"` \| `"competition"` | 运行目标级；日常推荐 `daily`，正式赛事选 `competition` |
| `intent` | `"survival"` | `"survival"` \| `"speedrun"` | 向 `SurvivalPlaybook` 提供阶段建议倾向；不绕过安全门禁，不解锁未绑定技能 |
| `sleep_policy` | `"opportunistic"` | `"opportunistic"` \| `"strict"` | `opportunistic` 仅在附近已有可用床时睡觉，无床夜/雷暴继续作业；`strict` 严格夜/雷暴拦截 |
| `operator_goal` | `""` | 字符串（<= 1000 字符） | 给模型的自然语言战术目标，不可含控制字符，安全门禁始终优先于它 |
| `world_id` | `"default"` | `[A-Za-z0-9_-]{1,64}` | 世界隔离标识；**切换新世界必须修改此值**，防止读取旧档检查点 |
| `state_dir` | `"runtime/survival"` | 目录路径 | 持久化检查点、SQLite 记忆与实例锁根目录 |
| `scan_radius` | `24` | 整数 `4..64` | 局部观察周边方块扫描半径（格） |
| `poll_interval_seconds` | `1` | 正数 | 每轮状态轮询/动作间默认等待秒数；反应更紧凑可设 `0.5` |
| `retry_seconds` | `5` | 正数 | 常规动作失败退避基准时间（秒） |
| `max_steps` | `256` | 整数 `1..10000` | 单次运行的全局步骤上限（`competition` 另有 `survival.competition.max_steps`） |
| `max_runtime_seconds` | `3600` | 正数 | 单次运行全局时长上限（秒）（`competition` 另有 `survival.competition.max_seconds`） |
| `max_target_failures` | `2` | 整数 `1..10000` | 同一坐标/目标连续失败几次后进入冷却 |
| `target_cooldown_seconds` | `300` | 正数 | 冷却目标禁止再选的时长（秒） |
| `max_consecutive_failures` | `6` | 整数 `1..10000` | 全局连续不可恢复失败次数上限，达到后安全暂停 |
| `max_deaths` | `3` | 整数 `1..10000` | 允许死亡总次数，超过后停机停止尝试 |
| `explore_distance` | `16` | 正数 `<= 32` | 单步小探索移动目标距离 |
| `max_explore_steps` | `8` | 整数 `1..10000` | 连续原子探索步数预算上限 |
| `low_health_threshold` | `8` | 正数 `<= 20` | 低于此生命值（1 颗心 = 2）触发治疗/进食/撤退门禁 |
| `food_threshold` | `16` | 正数 `<= 20` | 低于此饥饿值优先补充食物 |
| `threat_radius` | `8` | 正数 `<= 24` | 敌对生物近身警戒半径（格） |

### 4. `survival.autonomy`（目标程序闭环预算）

| 键 | 默认值 | 校验范围 / 类型 | 含义与调优说明 |
| --- | --- | --- | --- |
| `enabled` | `true` | 布尔 | 开启后允许 `run_survival_task` 本地多步自执行，大幅减少模型往返开销 |
| `local_opening` | `false` | 布尔 | 仅 `daily` 且无自定义 `operator_goal` 时生效：开局不问模型先本地准备 `starter_tools` |
| `max_steps` | `128` | 整数 `1..2048` | 单个 `run_survival_task` 子步骤上限 |
| `max_seconds` | `600` | 正数 `<= 3600` | 单个目标允许持续执行的最长秒数 |
| `max_local_failures` | `4` | 整数 `1..32` | 目标内部本地自救连续失败上限，耗尽后 `needs_ai` 交回大模型 |
| `max_no_progress_steps` | `8` | 整数 `1..128` | 目标内连续无实质进度上限 |
| `max_explore_steps` | `8` | 整数 `0..64` | 目标内探索移动步数上限；设为 `0` 禁用目标内探索 |

### 5. `survival.competition`（赛事专用控制）

| 键 | 默认值 | 校验范围 / 类型 | 含义与调优说明 |
| --- | --- | --- | --- |
| `eligibility_overrides` | `{}` | 映射 `{minecraft:id: bool}` | 显式确认或屏蔽 `rules_v2.json` 中的条件计分 advancement（键名必须合法且以 `minecraft:` 开头） |
| `defer_seconds` | `300` | 正数 `<= 3600` | `force_exit_task` 暂缓目标后过多久才允许重新选择 |
| `advancement_poll_seconds` | `5` | 正数 `<= 60` | 向服务端轮询 advancement 计分证据的最小间隔（秒） |
| `max_seconds` | `28800` | 正数 `<= 28800` | 比赛会话持久化总时长上限（默认 8 小时，本地会话预算，不等同于赛方服务器裁判计时） |
| `max_steps` | `100000` | 整数 `1..1000000` | 比赛模式总步骤预算上限 |

### 6. `survival.daily`（日常任务板储备阈值）

| 键 | 默认值 | 校验范围 / 类型 | 含义与调优说明 |
| --- | --- | --- | --- |
| `food_reserve` | `8` | 整数 `1..64` | 触发补给维护的食物目标件数 |
| `wood_reserve` | `8` | 整数 `1..64` | 原木等效储备目标件数 |
| `fuel_reserve` | `8` | 整数 `1..64` | 煤炭/木炭目标储备件数 |
| `maintenance_interval_seconds` | `30` | 整数 `1..60` | 日常任务板维护/状态刷新最小节拍间隔 |

### 7. `llm`（大模型接口与稳定性）

| 键 | 默认值 | 校验范围 / 类型 | 含义与调优说明 |
| --- | --- | --- | --- |
| `enabled` | `false` | 布尔 | 开启模型原生工具调用（`competition` 要求 `true`；报错不静默退回规则脚本） |
| `base_url` | `"http://127.0.0.1:8000/v1"` | HTTP(S) URL | OpenAI 兼容接口基址 |
| `api_key` | `""` | 字符串 | 接口密钥，优先通过环境变量 `BOTSLEEP_LLM_API_KEY` 注入 |
| `model` | `"qwen3.6-35b-a3b"` | 字符串 | 发往服务端的模型路由标识（实际身份以服务端 `response.model` 为准） |
| `temperature` | `0.3` | `0..2` | 采样温度；原生 `tool_calls` 建议保持 `0.1~0.3` |
| `max_tokens` | `2048` | 整数 `128..32768` | 单次模型输出 token 上限 |
| `timeout_seconds` | `60` | 正数 | **不要低于 `60`**：网络超时不重试，直接落入持久化 `uncertain` 锁防止并发重复调用 |
| `max_history_turns` | `10` | 整数 `1..64` | 本地保留历史交互轮数（超出 `30KB`/`48KB` 字节预算仍会触发自动旧轮裁剪） |
| `max_repair_attempts` | `1` | 整数 `0..2` | 参数/格式校验不通过时，原轮向模型补充错误提示自愈修正的次数 |
| `tls_version` | `"auto"` | `"auto"` \| `"1.2"` | 是否强制使用 TLS 1.2（证书与域名校验保持开启）；遇部分代理/网关断连可改 `"1.2"` |

### 8. `debug`、`mining`、`sleep` 与 `rcon`

- **`debug`**：
  - `enable_in_game_commands`（默认 `false`）、`command_prefix`（默认 `"#"`）、`allowed_usernames`（玩家白名单列表，为空时拒绝所有游戏内命令；`competition` 模式禁用聊天调试）。
  - `flight_recorder`：`enabled`（默认 `true`）、`output_dir`（默认 `"logs"`，输出脱敏 JSONL 飞行日志）。
  - `monitor`：`enabled`（默认 `false`）、`host`（必须为回环地址 `127.0.0.1`/`localhost`/`::1`）、`port`（默认 `18930`）、`heartbeat_seconds`（默认 `2`）、`console`（默认 `false`，是否向控制台同步打印事件）。
- **`mining`**：控制深度采集与矿洞安全策略（包括 `targets` 各矿需求量与最低镐、`cave_policy` 探洞洪水填充/视野/火把/重试预算、以及 `home_thresholds` 返家兜底阈值：`min_empty_slots: 3`、`min_health: 12.0`、`min_food: 10.0`、`min_torches: 4`、`min_blocks: 16`、`min_pickaxe_durability_ratio: 0.10`、`min_remaining_seconds: 30.0`）。
- **`sleep`**：仅 `agent.mode: sleep` 主用（`enabled`、`radius: 16`、`threat_check: true`、`threat_distance: 8`、`timeout_seconds: 600` 等）。
- **`rcon`**：仅用于**显式实服靶场测试**（`enabled: false`）；模型和正常生存循环绝不调用 RCON。

---

## 五、常用启动、恢复与防错命令

- **仅验证配置是否合法（不连接服务器）**：
  ```powershell
  .\.venv\Scripts\python.exe main.py --config match_config.yaml --check-config
  ```
- **中途暂停/断线后从最新状态恢复（绝不自动重放未知写操作）**：
  ```powershell
  .\.venv\Scripts\python.exe main.py --config match_config.yaml --mode survival --objective competition --resume
  ```
- **解除 `MODEL_REQUEST_UNCERTAIN` 锁（必须先人工确认模型服务端已无在途请求）**：
  ```powershell
  .\.venv\Scripts\python.exe main.py --config match_config.yaml --mode survival --objective competition --acknowledge-model-idle --resume
  ```
- **快速排坑对照**：详见 [DEBUG_GUIDE.md](DEBUG_GUIDE.md) 的“常见踩坑与排查清单”。

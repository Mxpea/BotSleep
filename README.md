# BotSleep

基于 mc-agent-api 的睡眠、生存与赛事机器人。默认模式在夜间或雷暴时找床并验证睡眠；`--mode survival` 支持 starter、daily 和 competition：starter 运行初期石器目标，daily 运行食物、工具升级、熔炼、床与住所、储物、死亡掉落恢复和持续维护，competition 按规则 advancement 目录选择得分任务并以服务器状态计分。生存模式支持确定性规划和可选模型规划；默认不调用模型，正常运行无需 RCON。

模型决策循环已接入原生工具调用、技能执行和结果反馈；赛事任务系统与只读赛事面板已接入，搭高、落地水与高频反射仍未完成。生存运行方式见 [SURVIVAL.md](docs/SURVIVAL.md)，睡眠契约见 [SKILL_SPEC.md](docs/SKILL_SPEC.md)；[ARCHITECTURE.md](docs/ARCHITECTURE.md) 还包含尚未实现的设计目标。

### 日常生存链路（2026-11-14）

按当前物品、方块、容器槽位和生命状态计算 13 项任务，不接受模型自报完成。新增作物收种、耕地、有界狩猎、铁矿采集、装备、熔炼装填与取出、箱桶存取、床与门、5×5 住所检查及分步施工；小批采集和施工最多 8 步，每步复核，遇到失败或未知结果立即停止。住所只验证结构，不能据此保证光照足够或完全防怪。

日常模式共注册 31 个工具。补上从短草/高草获取小麦种子的开局入口，随机未掉种子不计作取得种子；新增只清一格相邻可见树叶/藤蔓的局部清障，禁止挖自身支撑和头顶，清理后重新观察。遮挡消失、黄昏转白天或近敌离开会重新评估相应安全失败，普通寻路失败仍保留历史和冷却。

参考 `with-you.exe` 的事件反馈、任务标识和分层记忆思路，增加按世界与账号隔离的 SQLite 经验、地点和失败策略记忆。记忆有来源、期限和数量限制，只用于召回候选；执行仍依赖新鲜观察。上一动作的原生工具反馈会保留到下一次模型响应，日常任务会在死亡、物品损失或设施变化后重新计算。

生存 harness 串行执行一个技能，独立单模型 worker 与跨进程门禁串行化模型请求，慢模型期间本地安全观察和撤退仍可运行。客户端超时不能证明服务端已结束：此时持久化锁定后续模型请求，保留本地安全处理；不会通过重启或自动重试绕过。确认服务端请求已经结束后，才可使用 `--acknowledge-model-idle`。这与游戏动作未知结果需要 `--resume` 重新观察是两个独立恢复过程。

```powershell
python -B main.py --config config.yaml --mode survival --objective daily
```

配置中开启 `llm.enabled` 和 `debug.monitor.enabled` 后，可在本机监控查看任务板、记忆统计、模型原始返回、请求槽状态和技能证据。步骤、时间、死亡预算依然有效，到达预算会结束本次运行；不承诺无限无人值守。详细模块与限制见 [DAILY_SURVIVAL.md](docs/DAILY_SURVIVAL.md)；赛事配置与任务规则见 [HARNESS_INTERFACES.md](docs/HARNESS_INTERFACES.md)。

### 赛事得分任务链路（2026-11-15）

`survival.objective: competition` 需要 `agent.mode: survival`、`llm.enabled: true`，并关闭聊天调试。规则快照 `data/competition/rules_v2.json` 包含 125 项（120 个 advancement、5 个根节点）：45 项明确计分、54 项条件待确认、26 项不计分。只有服务器 advancement 状态可确认得分；未知不计分，条件项须经配置显式确认，归一化得分暂为 `null`。

Harness 每次只维护一个活动目标，提供完整目录与资源/技能可用性；模型决定先后与具体策略。当前已有明确行动计划的只有 7 个早期目标，其余可计分项保留在目录和监控中，但标为 unsupported，不会假称可执行。`finish_task` 只能在 fresh advancement 检测后回到选择；正常 `end_competition` 要 fresh 检测完整目录，未知目标可记录为 unknown 但不加分。终态写入检查点，重启后不再调用模型。当前不是完整赛事执行器，尚无所有成就的技能计划、阶段 skill 加载、compact/handoff 或水面/搭路导航。

```powershell
.\.venv\Scripts\python.exe main.py --config config.yaml --mode survival --objective competition
```

只读监控会呈现原始分、目标阶段、全目录评估新鲜度、模型请求槽、游戏 pending 和最近结果；监控方案与当前缺项见 [MONITORING.md](docs/MONITORING.md)。

## 已合并的主要改动

更新日期：2026-11-15；本节原合并记录是历史快照（`ed685f7`）。当前工作区基线为 `c21d623`，赛事任务改动尚未提交；当前验证见下方最新状态与 [TESTING.md](docs/TESTING.md)。

| 模块 | 本次改动 |
| --- | --- |
| 配置与 API 客户端 | 加载、合并和校验 YAML、环境变量与命令行参数；统一客户端与兼容入口，修复观察接口和业务错误处理；区分请求超时与明确失败，避免重复写操作。 |
| 自动找床睡眠 | 搜索并合并床头脚、跳过占用床、检查维度与危险、尝试接近候选床，验证真实入睡和醒来，并接入主入口。 |
| 初期生存技能 | 实现采集、制作、放置、进食、移动、撤退；结合睡眠技能，通过实际库存、方块或位置变化判断结果。 |
| 生存 harness | 每轮观察、规划、执行一个动作、验证并反馈；增加动作互斥、取消、失败退避、目标冷却、检查点及步骤、时间、探索和死亡预算。 |
| 模型自主规划 | 接入 OpenAI 兼容的原生工具调用；模型根据当前状态选择技能，收到成功、失败或部分完成结果后继续规划。只接受已实现的工具，执行前再次检查状态。 |
| 实时监控与日志 | 增加本机只读网页，展示模型请求和返回、工具参数、Agent 阶段、执行结果、错误、token 用量和耗时；持久化脱敏 JSONL 日志。 |
| 回归测试与文档 | 补充配置、客户端、睡眠、生存、模型协议、恢复和监控测试；记录离线结果与真实游戏验收范围。 |

### 针对实际运行问题的修复

- **高处资源接近**：采集会利用 API 提供的触达信息，避免仅因脚底到目标的距离而强制走到更近的位置。服务若明确报告遮挡，则拒绝直接挖掘并反馈原因。
- **原地移动误判**：移动结果明确包含 `moved`、`already_in_range` 和 `executed`。已处于容差范围内不代表完成脱困，也不会清空此前失败计数；模型规划会拒绝无位移的移动目标。
- **失败后调整**：失败动作记录不会被等待或原地操作冲掉；技能保留可用的错误诊断，并区分方块移除与实际取得掉落物。模型需换目标、换站位，或等待相关状态变化。
- **地形信息不足**：模型观察增加经本地检查的站位和放置候选，各最多 8 个；这些候选表示空间和支撑条件符合要求，不保证存在可行路径。观察期间跨格或明显升降会丢弃该次快照。
- **上下文超限**：历史观察和工具结果改为摘要，按完整工具事务裁剪历史；初期模式请求预算为 30,000 字节，日常模式为 48,000 字节，含工具定义。字节数不是精确 token 估计；最新未送达反馈不得被裁掉，无法容纳时明确拒绝请求。
- **原版 API 的观察限制**：缺少视线字段时，在 BotSleep 内通过有限方块查询保守判断遮挡；已挖未拾取时补充附近匹配掉落物的位置，供后续规划使用，不重发挖掘。制作返回非法 JSON 或无效响应仍按未知结果暂停。
- **API 1.3.7 适配**：`dig_unconfirmed` 后停止并有界复核方块、库存；确认延迟成功才报告成功，仍未知则暂停并保留 pending，绝不自动重挖。速度字段进入模型与监控，缺失或 `null` 显示未知。详见 [恢复契约](docs/SURVIVAL.md#未确认挖掘的复核)。
- **树冠撤退**：允许净空经过核验的两格下降，先找到通向实地的完整候选路线，再逐个相邻脚格移动和复核；树叶中继不算完成。按实际失败落点冷却，无路时等待新观察，死亡后清除旧撤退状态；未知结果仍暂停。详见 [技能契约](docs/SURVIVAL.md#技能契约)。

本分支仅修改 **BotSleep**，适配原版 **mc-agent-api**，不要求修改相邻 API 仓库。此前本地 API 实验补丁已撤回，未提交或上传。`can_dig`、`can_see`、`collection` 等额外元数据按可选字段处理；早期验收使用的原版 API（`e11f2f3`）没有前述视线字段；后续 `1cfecc3` 的独立实测见下表。原版 API 缺少这些字段时，BotSleep 用局部体素查询补充保守判断，未知形状或读取预算不足不会当作可见。该判断不是完整碰撞模型，不能保证消除底层寻路卡住，也不等于天然地图长期生存已通过。

### 当前验证状态（2026-11-15，工作区基线 `c21d623`）

**本轮全套离线测试：839 项，25.143 秒，1 项 ERROR；赛事定向回归与监控定向回归通过。** 唯一 ERROR 是 Windows 临时目录清理 `memory.sqlite3` 时的 WinError32/267；不是业务断言失败，但全套不算全绿。完整证据、复现命令与边界见 [TESTING.md](docs/TESTING.md)。本轮没有连接游戏、API、RCON 或模型，也没有实服赛事验收。

| 范围 | 已有证据与边界 |
| --- | --- |
| 离线自动化 | 最新全量 839 项，1 项 ERROR（Windows 临时 SQLite 清理），不能算全绿；赛事及监控定向回归通过，见 [TESTING.md](docs/TESTING.md)。 |
| 赛事任务系统 | 125 项规则目录、fresh advancement 证据计分、pending/excluded 不计分、任务强退/终止、终态恢复与只读赛事面板均有离线覆盖；仍只有 7 个早期计分目标有明确计划，未完成实服验证。 |
| 自动睡眠 | 已有独立测试世界的真实找床、入睡和醒来验收记录，详见 [TESTING.md](docs/TESTING.md)。 |
| 确定性生存规划 | 此前在独立、和平难度的平坦靶场，从空背包完成初期石器工具链；不代表模型规划在天然地图上的同等能力。 |
| 真实模型接入 | 已收到 `default` 路由的原生工具调用，响应 `model` 为 `qwen3.6-35b-a3b`，实际动作失败结果已回传。这是当时服务自报的模型标识，不保证该路由以后固定指向同一模型。 |
| API 1.3.7 适配 | 历史记录基于官方 `1cfecc3`，新版状态字段曾实际观察；BotSleep 不依赖相邻 API 仓库的本地补丁。`dig_unconfirmed` 有界复核、未知结果保留、树冠分段撤退和日常生产技能有离线覆盖；历史实测未自然触发 `dig_unconfirmed`，见 [TESTING.md](docs/TESTING.md)。 |
| 自然地图自主采集与制作 | 2026-11-14 15:50–15:56，原版 API `1cfecc3` 下同会话真实完成清叶、两次采木、制板、制木棍和木镐；目标方块或库存变化已确认。工作台制作缺料、移动未到达后，失败结果以配对工具消息返回模型，模型改换落点、补采并制作。16:03 夜间撤退期间被僵尸击杀，重生后继续观察；完整石器及长期日常生存仍未验收。 |

较早同会话的后端失败恢复证据（2026-11-14，UTC）：**04:56:40** 制作木镐返回 `missing_ingredients`；**04:56:47** 下一模型请求带对应的失败工具反馈；**04:57:19** 模型改为采木；**04:57:27** 原木库存增加 1，目标方块已移除。此前的 `llm_no_progress` 则是本地在执行前拒绝原地移动，再让模型修复调用，不能与后端实际执行失败混为一谈。

上段04:56–04:57历史证据对应 `mc-agent-api` **`e11f2f3` 原版**；表内15:50–15:56是新版 `1cfecc3` 的独立实测。两次运行之间仅继承检查点失败上下文，没有恢复旧原生工具消息历史；同会话请求的工具配对另行验证。详细时间线见 [TESTING.md](docs/TESTING.md)。原始报告位于本地忽略目录 `.test-runtime/`，要塞历史证据另见 `logs/`；两者均不随 Git 拉取分发，接收端需取得脱敏副本或重新验证。

## 安装与配置

Python 3.10+，在本目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

编辑配置中的 `mc_api.base_url`、`agent.name`、`agent.password`，与 mc-agent-api 的认证配置匹配。用户名必须被服务端允许。配置与口令不应提交 Git。

配置优先级：

- 配置文件路径：`--config` > `BOTSLEEP_CONFIG` > 项目目录中的 `config.yaml`。
- 用户名：`--username` > `MC_AGENT_USER` > YAML > 默认值。
- API 地址与口令：`MC_AGENT_API` / `MC_AGENT_PASSWORD` > YAML > 默认值。
- 模型地址、密钥、请求路由：`BOTSLEEP_LLM_BASE_URL` / `BOTSLEEP_LLM_API_KEY` / `BOTSLEEP_LLM_MODEL` > YAML > 默认值。
- 日志目录相对配置文件解析，不依赖启动命令的工作目录。

```powershell
.\.venv\Scripts\python.exe main.py --config config.yaml --check-config
.\.venv\Scripts\python.exe main.py --config config.yaml
```

`--check-config` 只检查配置，不连接服务器。正常运行前需要先启动 Minecraft 和 mc-agent-api。旧命令 `python bot_sleep.py` 现复用相同入口和配置。

只运行一次有时间上限的睡眠尝试：

```powershell
.\.venv\Scripts\python.exe main.py --config config.yaml --once
```

成功退出码为 0；无床、白天、中断、超时、配置或连接失败均为非零。睡眠持续模式在白天保持观察，失败后按 `sleep.retry_seconds` 冷却再尝试。

## 初期生存模式

```powershell
.\.venv\Scripts\python.exe main.py --config config.yaml --mode survival --max-steps 128
```

也可配置 `agent.mode: survival`。每轮读取新状态、背包、资源与实体，只执行一个经过参数校验的技能，然后用背包/方块/位置变化验算结果。目标为从木材开始合成工作台、木镐，采集圆石，获得石镐、石斧、石剑和熔炉；已有煤或木炭时可制作火把。

生存 harness 包含单动作互斥、既有食物进食、近处怪物撤退尝试、夜间已有床睡眠、死亡后重新观察规划、断线时复用会话重连、失败退避与目标冷却、探索/步骤/时间/死亡次数限制。未知写操作结果会暂停，恢复时先观察，绝不从存档直接重放动作。`#stop` 暂停、`#resume` 继续、`#status` 查询任务状态。

重启后需要明确恢复时，使用 `python main.py --config config.yaml --mode survival --resume`；该选项先重新观察再规划。默认启动及未带 `--resume` 的 `--once` 均不会绕过未完成动作的暂停。

检查点与 JSONL 事件保存在 `survival.state_dir`，按 API 地址、账号、`survival.world_id` 隔离。**服务器更换世界时必须修改 world_id**，API 本身不提供世界存档身份。动作执行前保存意图；进程异常退出后遗留意图会阻止自动重启，需明确恢复。日志不使用受版本控制的 `data/game_logic/`。

持续运行到目标完成退出 0，步骤/时间预算耗尽退出 2；`--once` 只执行一次观察决策，退出 0 仅说明该步成功（也可能为等待），不代表完整目标完成。普通连续失败或未知动作结果会保持连接并暂停，等待调试恢复；模型瞬时错误按下述策略退避。未开启调试时可 Ctrl+C 退出再检查日志。

以上描述 `starter` 初期工具链；它不启用食物获取、狩猎、熔炼、床与住所等 `daily` 技能，也不代表 competition 的 advancement 计分闭环。日常模式的实现与验证边界见 [DAILY_SURVIVAL.md](docs/DAILY_SURVIVAL.md)，通关规划仍未接入。危险地形没有已确认的逃生办法时会等待；不保证实时救援。采集只允许指定木石煤类型，但 API 无法区分玩家放置与自然生成。工具调用返回、移动达到容差和真正取得物品是不同证据，不能据此互相代替；天然地形中的寻路、采集和制作需逐项验收。

## 模型规划与本机监控

在配置中同时设置 `agent.mode: survival` 和 `llm.enabled: true`，填写支持原生工具调用的 OpenAI 兼容 `llm.base_url`、服务允许的 `llm.model` 路由及密钥。密钥可通过 `BOTSLEEP_LLM_API_KEY` 提供；启用后会实际请求模型服务，费用取决于该服务。

复制示例配置后，修改 `config.yaml` 中对应字段（下面是节选，不要重复追加同名 YAML 节点）：

```yaml
agent:
  mode: survival

llm:
  enabled: true
  base_url: "https://your-model-provider.example/v1"  # 替换为实际服务地址
  model: "default"                                # 使用服务允许的路由
  api_key: ""                                     # 通过 BOTSLEEP_LLM_API_KEY 提供
  timeout_seconds: 60
  max_tokens: 2048
  max_history_turns: 10
  tls_version: "auto"

debug:
  enable_in_game_commands: true
  allowed_usernames: ["YourPlayerName"]             # 替换为可信玩家名
  monitor:
    enabled: true
    host: "127.0.0.1"
    port: 18930
    console: true
```

保留并填写原配置中的 Minecraft API 地址、机器人账号和口令。模型服务如需 TLS 1.2 兼容，可设置 `llm.tls_version: "1.2"`；证书验证仍保持开启。完成配置后运行：

```powershell
.\.venv\Scripts\python.exe main.py --config config.yaml --check-config
.\.venv\Scripts\python.exe main.py --config config.yaml --mode survival
```

每次新鲜观察只接受一个原生 `tool_calls` 动作，经参数白名单校验后再次读取状态、检查危险和目标，再交给本地技能。技能的真实成功/失败及新观察通过对应 `tool_call_id` 的工具结果反馈给下一轮。模型不能关闭安全检查，也不能仅靠文字宣称目标完成。

完整收到 HTTP 408/425/429 或 5xx 时指数退避，上限 60 秒，仍受步骤与总时间预算限制。模型传输超时或网络中断会持久化未知状态并阻止后续请求，确认服务端空闲后才能显式解除；日常模式仍保持本地安全处理。不可重试的 HTTP 错误（如认证失败）会暂停；非法工具输出只进行有界修复。模型故障不会悄悄切换为规则采集。游戏写操作结果未知时按独立恢复规则暂停，不自动重发。

设置 `debug.monitor.enabled: true` 后，打开启动时打印的地址，默认是 `http://127.0.0.1:18930`。页面只绑定本机，显示观察/思考/复核/执行/等待阶段、暂停原因、位置与生命、决策和技能结果，以及脱敏模型请求/返回、工具调用、服务提供的附带推理字段、token 和耗时。事件每 0.5 秒轮询，内存保留最多 500 条，超大内容有截断标记；持久事件由 FlightRecorder 写入日志目录。监控为只读，控制仍通过可信玩家的 `#stop` / `#resume`。

“请求路由”显示配置值（例如服务允许的 `default`）；“实际模型”仅显示响应中的 `response.model`，服务未返回时明确显示“未确认”。路由名或 `/models` 列表不能证明实际底层型号；响应标识也是服务自报信息。

## 自动睡眠行为

1. 确认机器人已就绪、存活且位于主世界，下界和末地拒绝使用床。
2. 查询 16 种颜色的床，合并床头和床脚，跳过占用床。
3. 检查附近敌对生物；危险时明确失败，不调用未实现的搭高或撤退技能。
4. 按距离尝试接近床，不可达时尝试下一张；交互前再次检查状态和床位。
5. 验证入睡并等待醒来。只有观察到睡眠到醒来，或服务端确认 slept 且昼夜/雷暴状态发生转换，才返回成功。

默认搜索半径 16 格、单次移动 15 秒、入睡确认 5 秒、完整任务 600 秒。多人服务器可能需要等待其他玩家睡觉或自然天亮。

HTTP 超时不能证明服务端动作停止。客户端不自动重发写操作，技能会请求停止并返回错误。取消已入睡任务不会盲目再次点击床，机器人可能仍在床上，正常退出会登出。

## 调试与日志

将玩家名写入 `debug.allowed_usernames`，通过 `--debug` 或配置启用调试。支持标准 `<Player> #command` 聊天格式：

- `#help`：显示已实现命令。
- `#status`：显示位置、生命值和睡眠状态。
- `#stop`：请求取消当前任务，并暂停自动尝试。
- `#sleep`：恢复自动尝试并请求一次睡眠。
- `#resume`：仅生存模式，解除暂停后重新观察。

空名单拒绝远程命令；插件自定义聊天前缀需要另行适配。停止请求不是所有底层动作均已终止的保证。

睡眠运行在单个后台工作线程，主线程保持调试轮询。日志包含启动、睡眠开始、结果、停止请求和退出，结果包含床位、错误码和验证证据。详见 [DEBUG_GUIDE.md](docs/DEBUG_GUIDE.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

测试覆盖配置、API 契约、睡眠与生存技能、确定性/模型规划、原生工具结果反馈、安全门禁、取消/恢复、持久化、调试、监控和入口。离线集成测试使用本机临时 HTTP 假服务，不连接 Minecraft 或真实模型服务，也不改变游戏世界。

会布置场地、发物或传送的自动化验收必须使用独立测试世界；普通世界只进行用户授权的自然生存运行与观察。范围与结果见 [TESTING.md](docs/TESTING.md)。开发期 RCON 工具才需要：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 要塞计算（Ninjabrain Bot）

独立集成官方 1.5.2 的无界面计算核心，可从投眼坐标射线输出候选区块与概率。先用合成数据离线验证：

```powershell
.\.venv\Scripts\python.exe -m integrations.ninjabrain setup
.\.venv\Scripts\python.exe -m integrations.ninjabrain calculate --input data/examples/stronghold_throws.json
```

本轮合成示例 CLI 计算成功，首选区块为 `[128,0]`；这不是实际世界的定位结果。

`calculate_stronghold` 的 schema 单独保存在 `STRONGHOLD_TOOL_DEFINITION`；纯计算 Python Skill 与 CLI 可用，但它没有进入 starter/daily 的 9/31 工具目录，也未接入日常模型分派。

真实投眼采样、误差假设和限制见 [要塞计算集成指南](docs/STRONGHOLD.md)。采样会消耗末影之眼；2026-11-13 已有 5 次真实投眼采样与多观测计算实测记录；尚未挖掘确认传送门房。

## Harness 接口设计

[接口契约与架构决策](docs/HARNESS_INTERFACES.md) 持续记录底层 API 事实、赛事接口和架构访谈结论。competition 已接通规则目录、目标选择、服务器 advancement 计分、fresh 全目录结束检测、检查点恢复和只读监控；语义 compact/handoff、完整 reflex、水面导航与自动浮水/搭桥仍属提案，不能作为已实现工具调用。

## 代码入口

- `core/config.py`：配置加载与校验。
- `core/client.py`：唯一 HTTP 客户端；根 `client.py` 仅兼容导出。
- `skills/sleep.py`：床位搜索、交互、结果验证。
- `core/sleep_controller.py`：单任务调度、重试间隔与取消。
- `core/survival_harness.py`：生存观察、决策、技能分派、恢复和预算。
- `core/survival_state.py`：新鲜观察、按世界隔离的原子检查点与事件日志。
- `agent/survival_planner.py`：可复现的初期石器目标规划器。
- `agent/llm_planner.py`、`agent/brain.py`、`agent/tools.py`：原生模型请求、技能工具契约、输出校验和真实结果反馈。
- `skills/survival.py`：采集、合成、放置、进食、移动和撤退的验证技能。
- `debug/monitor.py`、`debug/monitor.html`：订阅脱敏事件的本机只读监控。
- `main.py`：配置装配、运行和退出清理。
- `tests/`：不依赖真实游戏的自动化回归。

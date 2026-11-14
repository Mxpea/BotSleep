# BotSleep

基于 mc-agent-api 的睡眠与初期生存机器人。默认模式在夜间或雷暴时找床并验证睡眠；`--mode survival` 运行有界的初期石器目标。生存模式支持确定性规划和可选模型规划；默认不调用模型，正常运行无需 RCON。

模型决策循环已接入原生工具调用、技能执行和结果反馈；搭高、落地水与高频反射仍未完成。生存运行方式见 [SURVIVAL.md](docs/SURVIVAL.md)，睡眠契约见 [SKILL_SPEC.md](docs/SKILL_SPEC.md)；[ARCHITECTURE.md](docs/ARCHITECTURE.md) 还包含尚未实现的设计目标。

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
python main.py --config config.yaml --check-config
python main.py --config config.yaml
```

`--check-config` 只检查配置，不连接服务器。正常运行前需要先启动 Minecraft 和 mc-agent-api。旧命令 `python bot_sleep.py` 现复用相同入口和配置。

只运行一次有时间上限的睡眠尝试：

```powershell
python main.py --config config.yaml --once
```

成功退出码为 0；无床、白天、中断、超时、配置或连接失败均为非零。睡眠持续模式在白天保持观察，失败后按 `sleep.retry_seconds` 冷却再尝试。

## 初期生存模式

```powershell
python main.py --config config.yaml --mode survival --max-steps 128
```

也可配置 `agent.mode: survival`。每轮读取新状态、背包、资源与实体，只执行一个经过参数校验的技能，然后用背包/方块/位置变化验算结果。目标为从木材开始合成工作台、木镐，采集圆石，获得石镐、石斧、石剑和熔炉；已有煤或木炭时可制作火把。

生存 harness 包含单动作互斥、既有食物进食、近处怪物撤退尝试、夜间已有床睡眠、死亡后重新观察规划、断线时复用会话重连、失败退避与目标冷却、探索/步骤/时间/死亡次数限制。未知写操作结果会暂停，恢复时先观察，绝不从存档直接重放动作。`#stop` 暂停、`#resume` 继续、`#status` 查询任务状态。

检查点与 JSONL 事件保存在 `survival.state_dir`，按 API 地址、账号、`survival.world_id` 隔离。**服务器更换世界时必须修改 world_id**，API 本身不提供世界存档身份。动作执行前保存意图；进程异常退出后遗留意图会阻止自动重启，需明确恢复。日志不使用受版本控制的 `data/game_logic/`。

持续运行到目标完成退出 0，步骤/时间预算耗尽退出 2；`--once` 只执行一次观察决策，退出 0 仅说明该步成功（也可能为等待），不代表完整目标完成。普通连续失败或未知动作结果会保持连接并暂停，等待调试恢复；模型瞬时错误按下述策略退避。未开启调试时可 Ctrl+C 退出再检查日志。

这是一条初期工具链，**还没有食物获取、狩猎/战斗、熔炼、造床、建屋、长期基地和通关规划**。危险地形没有已确认的逃生办法时会等待；不保证实时救援。采集只允许指定木石煤类型，但 API 无法区分玩家放置与自然生成。工具调用返回、移动达到容差和真正取得物品是不同证据，不能据此互相代替；天然地形中的寻路、采集和制作需逐项验收。

## 模型规划与本机监控

在配置中同时设置 `agent.mode: survival` 和 `llm.enabled: true`，填写支持原生工具调用的 OpenAI 兼容 `llm.base_url`、服务允许的 `llm.model` 路由及密钥。密钥可通过 `BOTSLEEP_LLM_API_KEY` 提供；启用后会实际请求模型服务，费用取决于该服务。

每次新鲜观察只接受一个原生 `tool_calls` 动作，经参数白名单校验后再次读取状态、检查危险和目标，再交给本地技能。技能的真实成功/失败及新观察通过对应 `tool_call_id` 的工具结果反馈给下一轮。模型不能关闭安全检查，也不能仅靠文字宣称目标完成。

持续模式遇到模型超时、网络错误、HTTP 408/425/429 或 5xx 时指数退避，上限 60 秒，仍受步骤与总时间预算限制。不可重试的 HTTP 错误（如认证失败）会暂停；非法工具输出只进行有界修复。模型故障不会悄悄切换为规则采集；进食、避险和睡眠等本地安全门禁仍可介入。游戏写操作结果未知时按恢复规则暂停，不自动重发。

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
python -B -m unittest discover -s tests -v
```

测试覆盖配置、API 契约、睡眠与生存技能、确定性/模型规划、原生工具结果反馈、安全门禁、取消/恢复、持久化、调试、监控和入口。离线集成测试使用本机临时 HTTP 假服务，不连接 Minecraft 或真实模型服务，也不改变游戏世界。

真实游戏验收必须使用独立测试世界，范围与结果见 [TESTING.md](docs/TESTING.md)。开发期 RCON 工具才需要：

```powershell
python -m pip install -r requirements-dev.txt
```

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

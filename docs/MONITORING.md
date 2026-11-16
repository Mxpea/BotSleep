# 只读运行监控

启用 `debug.monitor.enabled: true` 后访问本机 `http://127.0.0.1:18930`（默认）。监控只消费运行时已发布事件，不读游戏/API、配置或日志，不启动模型请求。网页每 0.5 秒 GET 本机状态和事件；连接重试仅刷新监控数据，不是模型或游戏动作重试。

赛事通过 `COMPETITION_TASKS data.board` 发布整板，`HEARTBEAT` / `AGENT_STATE data.competition` 可补充同一快照（也兼容 `{board: snapshot}`）。监控状态保存于 `competition.board`，不改变 `daily.board`、日常任务评估或记忆语义。`objective: competition` 时显示赛事面板；现有 `AGENT_PHASE` 继续表示运行阶段。

页面显示 selection/active/completed、`active_goal_id`、进度卡、规则版本、原始分、已完成数量、全目录检测/明确状态计数及 difficulty counts，并保留完整规则 ID。资格 `scored/pending/excluded` 分别表示可计分/待条件确认/不计分；可用性 `ready/needs_prerequisites/unsupported/unknown` 单独展示，评估 `completed/not_completed/unknown` 表示已完成/未完成/未确认。blocked 和其他未知值保留原文。缺失字段显示未确认，`normalized: null` 不换算为零，不根据分值、可用性或进度推算完成；可展开查看已发布的 completion 证据、last_exit、message 和原始快照。

| 情况 | 如何看 |
| --- | --- |
| 正常 | heartbeat 新鲜，观察 AGENT_PHASE、当前目标与进度变化。模型 idle 表示无在途请求，inflight 表示等待返回；游戏 busy 与模型槽独立。 |
| 暂停 | 看 paused、pause_reason、last_exit；busy 仍可能为真。监控持续接收事件不代表自动动作已恢复。 |
| 疑似卡住 | heartbeat 超过 15 秒显示 stale，这是浏览器时间阈值，不是故障判定。心跳时间单独记录，其他事件更新不会刷新它；页面失联时保留旧数据并继续计算时间。心跳新鲜但目标长期不变时查看任务前置和最近结果。 |
| 未知 | uncertain 表示模型请求结果不明，须保留持久化未知状态；游戏 pending 表示尚待核验的意图，缺失则显示未确认。两者都不会因刷新、超时或心跳恢复而被监控清除。 |

快照 `updated_at/stale` 与 heartbeat 分开展示，stale 只展示运行时标记；缺失时间不伪装新鲜。所有赛事字段复用 EventMonitor 脱敏与有界存储：最多 500 条事件，默认 8 MiB 缓存，单条最多 1 MiB（也受总预算约束），另有字符串、数组和嵌套上限。过大事件只留截断提示，面板可能仍保留前一有效快照，应结合时间线判断。

## 本地执行与模型接管

`SurvivalHarness.snapshot()` 的 `local_execution` 摘要包含 phase、reason、steps、recent（最近最多6条 action/success/error_code/progress）。running 表示本地推进，model_takeover 表示本地无可用方案或预算耗尽待模型接管，blocked_unknown 表示动作结果不明；尚未初始化时字段可为 null，不推断为成功或已停机。模型槽、pending 和本地执行状态分别核对。

`LOCAL_EXECUTION_STEP` 记录待执行意图，`LOCAL_EXECUTION_RESULT` 记录真实 SkillResult 与摘要；`MODEL_TAKEOVER` 记录请求接管的摘要，不证明模型已返回或任务完成。安全拒绝可见 `LOCAL_EXECUTION_YIELDED`。`EventMonitor` 从上述事件的 summary，或 `HEARTBEAT` / `AGENT_STATE` 的 local_execution 复制摘要到 `state.local_execution`，recent 最多6条；只投影已发布数据，不补造结果。

`GET /api/state` 顶层提供 `local_execution` 和 `decision_source`，供外部只读消费者使用。HTML 的“本地优先执行”卡片显示本地阶段、步数、接管/受阻原因与近期动作；“当前决策”显示来源。`state.decision_source` 只来自 `DECISION.data.source`：local/model/manual/safety/rules 分别显示本地/模型/手动/安全/规则，缺失或非法值显示未确认；不根据 planner=llm、MODEL_TAKEOVER 或历史事件推断来源。卡片和时间线均只读，不提供恢复、重试或清锁控制。

默认 `survival.local_execution` enabled=true、max_steps=64、max_seconds=180（秒），上限128步/600秒，每步新观察并沿用原安全门禁、pending 与串行技能。动作未知保留 pending，模型门禁未知或请求在途时不本地生产；接管不是 provider 错误 fallback。完整 starter 链路离线验证通过，零模型请求。本轮最终结果以 [TESTING.md](TESTING.md) 首节为准；未做实服验收，监控可见不代表真实技能成功或完整比赛完成。

失败位置保存在内部 `context.local_execution.failed_targets`，不在本地执行投影字段中；harness 先换其他已观察资源目标，不能从卡片缺少坐标推断没有换目标。主循环的 `next_poll_seconds` 让busy/无退避的本地ready最多50ms唤醒，本地成功中间步骤不等普通poll；失败退避与暂停保持原策略，聊天另按原间隔节流。快tick不改变网页刷新、heartbeat或聊天轮询间隔，也不是20Hz实时反射。

树冠分段下降在本地近期动作中显示为 `descend_canopy`；`LOCAL_EXECUTION_RESULT` 的结果含 `block_removed`、`support`、`action_state` 和 `evacuation_stage`。`relay` 只表示本次落脚已核验，需要重新观察续行；实心地面恢复确认可见 `CANOPY_LANDED`。意图事件和 `block_removed=true` 均不代表已经下树，最终以落地证据及恢复意图清除为准。

## 等待与夜间准备

`wait` 的成功只表示等待已执行，不代表任务前进。查看最近结果原因、survival_constraints 及当前观察：`shelter_time` 可能优先回家/睡眠/留在室内，或因危险/观察不足暂不允许准备；`night_preparation` 则允许本地或模型选择附近单步准备。黄昏拒绝旧批量动作会反馈 `night_preparation_single_step_only`，随后可重新规划。上述字段由已有模型请求/动作事件显示，不新增控制端点或自动清锁。

## 网页打不开与运行中补看日志

先看启动输出是否有 `Live monitor: ...`。`debug.monitor.enabled: false` 时不会启动网页；改成true需要下次启动加载。端口被其他进程占用也不能重复启动监听器。回环地址只在运行BotSleep的机器上可访问，不是其他机器或手机的服务器地址。

不重启机器人时，可以启动独立日志查看器：

```powershell
python -B -m debug.monitor_tail --log-dir logs --port 18931
```

访问 `http://127.0.0.1:18931`。日志目录也可用 `--file logs/flight_YYYYMMDD.jsonl` 指定一个文件；具体参数以CLI帮助为准。它只补读指定flight日志、复用脱敏和有界事件面板，不登录游戏、不请求模型、不读取私有配置、不提供控制或文件下载接口。日志页面不与主程序默认18930端口竞争。

页面“数据来源”显示只读日志补读和最近实际日志事件时间。日志没有HEARTBEAT时保持未确认，不能把查看器活着当作机器人在线；旧日志、已经结束的请求或动作也不会因补读变成正在运行。模型原始工具参数/报错、阶段、生命/位置和最近结果用于定位卡点；pause、pending、slot未知仍分别显示，页面不清锁、不重放动作。关闭该查看器只停止网页，不影响机器人。

## 受阻与显式尝试的审计

`BLOCKAGE_REPORTED` 记录原因、attempts和任务是否退出；`FORCED_ATTEMPT_RESULT` 记录请求动作、理由和真实SkillResult；`CANOPY_PREPARATION` 只证明清障后需要重观测，不是已脱困。网页仍只展示发布事件，无force/清锁/执行端点。模型请求日志中的tools是本轮实际提供集合；注册目录数量大于本轮可选数量是预期行为。强制动作不代表必成功，未知写入仍显示pending并停止继续派发。

树冠恢复的请求tools可能出现临时 `force_clear_foliage`，但STEP_RESULT中的canonical decision仍是force_attempt，这是同一操作的两种表示，不是重复调用。看该轮工具描述与context.recovery.bound_action确认绑定的实际坐标；`{}`对这个别名是有效选择，对通用force_attempt仍不合法。HTTP禁止parallel_tool_calls，执行层仍只派发一个动作。

## 模型候选与审计目录

2026-11-16 起，模型只接收 `CompetitionTasks.model_view()` 的阶段候选摘要，选择后展开单任务详情；本页面仍显示 `snapshot()` 的完整 125 项规则目录。面板显示某项目不代表当前模型可选。`selectable` 同时受当前资源阶段、计分资格、技能可用、已确认状态和 cooldown 约束；最终得分与正常结束仍对完整非根目录检测。阶段投影不清除 pending 或模型未知锁。

## 门户调试记录与系统监控的区别

最新合并加入 `skills/portal_telemetry.py` 的 `PortalTelemetryRecorder`：`record_step(...)` 会主动读取机器人、目标方块和可选门框矩阵，也可通过 `action_func` 执行动作；`dump_json(filepath=None)` 默认写 `logs/telemetry/trial_<trial_id>.json`。这些是独立门户靶场证据，未接入 `EventMonitor` 或赛事计分，不是只读监控接口。

`LavaLakeGenerator` 和随机岩浆矩阵脚本会通过 RCON 改地形/物品/位置，仅用于专用测试世界。文件中的零伤害、水不外溢和通过率是测试目标；本轮只验证默认门禁，不声称这些目标已在新版本实服达成。导出的 JSON 不自动继承 FlightRecorder 脱敏或容量限制，分享前需检查。

## 状态监控建议与当前缺口

不要用单个“健康”灯把不同故障合并。至少分开看以下层次，且每个值保留自己的时间戳/来源：

| 层次 | 当前可观察信号 | 解释 / 后续建议 |
| --- | --- | --- |
| 进程存活 | `heartbeat_at`、Agent phase、step、paused | 心跳新鲜只证明主循环持续汇报；不证明动作、模型或完整观察正常。15 秒 stale 是提示，不是自动停机判据。 |
| 游戏状态 | heartbeat 的位置、生命、`busy`、run.pending | 游戏动作 busy 与模型槽独立；pending/结果未知须醒目显示，禁止由刷新触发重试。可补 API 最近成功读取时间与按端点延迟/错误率。 |
| 目标与计分 | competition `phase`、active goal、原始分、`catalog_assessment`、`advancements.stale` | `known_count < total` 或 unknown 不等于已完成；score 只来自确认的服务器 advancement。可补“上次分数/任务进度变化时间”，用于识别无进展但不抢占模型决策。 |
| 模型服务 | `slot_state` idle/inflight/uncertain、实际响应模型、耗时、token | uncertain 是必须人工/运维核实的远端状态，不是普通错误。未来可按 endpoint 指标汇总 p50/p95 延迟、明确 HTTP 错误和传输未知。 |
| 持久化恢复 | checkpoint 内容、pending 与 terminal 状态 | 当前可见 pending 和任务状态，但没有单独的“最近成功写检查点/写入失败”指标。建议记录原子写入时间、失败码、scope 摘要（不含凭据）。 |

可靠性规则建议先“告警、不自动改动作”：Heartbeat stale、advancement evidence stale、模型槽 uncertain、游戏 pending、checkpoint 写入失败应分别显示不同等级与处置；连续无进度只能提示检查目标前置/重复动作，不能仅凭时长宣判 attention collapse。确认误报率后再加策略性自动暂停/compact；无论如何监控 API 始终只读，不调用动作、不清锁、不正常化未知值。

只有 `/`、`GET /api/state`、`GET /api/events?since=N`，没有控制、write、admin、解除锁端点。页面不触发游戏读取、动作重放或模型自动重试，也不实现自动 compact。只读监控和离线测试无法证明连续 8 小时可靠性，仍需独立长时实服验证。

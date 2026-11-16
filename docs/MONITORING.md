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

## 程序长任务

`task_execution` 是独立于日常任务板和赛事得分板的运行快照。页面显示高层任务/ID、发布状态、`progress.current/target/unit`、当前子步骤、步骤预算与最近最多 8 条原子动作结果。状态为 `idle/running/suspended/completed/needs_ai/unknown/cancelled`；`fresh=false` 显示旧快照待复核。单步 `success`、进度达到目标或 `TASK_COMPLETED` 事件名称本身都不会让前端推断任务完成，缺失值显示未确认。

| 发布字段 | 页面含义 |
|---|---|
| `local_recoveries` | 程序处理的本地失败恢复累计次数，不代表每次都已恢复成功。 |
| `emergency_interruptions` | 目标被本地应急挂起的累计次数。 |
| `ai_escalations` / `last_escalation_reason` | 升级询问 AI 的累计次数与最近原因；尚未再次请求模型时仍可有升级状态。 |
| `model_planning_rounds` | **AI 规划轮次**：同检查点累计的模型规划 worker 启动次数，重启不归零；一次格式 repair 可产生额外 HTTP 请求，不能当作本次登录的 HTTP 请求总数。 |
| `model_calls_avoided` | **程序子步骤**：已授权目标下由程序选出的子步骤数量；不等于实际成功动作数，也不是测量出的费用节省。 |

`HEARTBEAT/AGENT_STATE` 与 `TASK_STARTED/TASK_PROGRESS/TASK_SUSPENDED/TASK_COMPLETED/TASK_NEEDS_AI/TASK_UNKNOWN` 携带完整同结构快照，前端和事件投影均不重复累加计数。快照继续使用统一脱敏、限长与只读 GET；没有恢复任务、清除 unknown 或重放子步的网页按钮。目标完整契约见 [AUTONOMOUS_TASKS.md](AUTONOMOUS_TASKS.md)。

模型请求原始 JSON/时间线还保留 `LLM_REQUEST.tool_mode` 与 `atomic_repair_remaining`，用于检查本轮是目标规划还是有界原子修复；它们不是新的控制接口，也不代替任务完成证据。

## 生存阶段与战斗证据

`HEARTBEAT` / `AGENT_STATE` 发布的 `playbook` 和 `combat` 原样进入只读状态；短安全观察将旧阶段建议标记 `fresh: false`，页面显示“旧阶段建议 · 待完整观察”。阶段、缺少前置与未绑定能力是规划提示，不是完成证明。`survival.intent: speedrun` 也不会自动绑定门户、下界导航或击龙技能。

`STEP_RESULT` 中 `hunt_animal`、`fight_hostile`、`hunt_with_lava`、`collect_liquid` 及内部 `release_defense` 的最近结果保存为 `combat_result`，包含技能、发布时间与原始结果。页面区分攻击已发、伤害/击杀证据、盾指令、岩浆回收和撤退需求；未发布字段显示“未确认”。普通成功不会推导击杀，旧结果不会冒充当前状态。字段继续采用同一脱敏、大小限制与纯 `textContent` 渲染，没有新增动作或控制端点。

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

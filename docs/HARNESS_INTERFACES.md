# Harness 接口契约与架构决策

更新：2026-11-16；`3801be9` 为此前合并核对基线，当前还包含目标程序扩展。状态：**第 2 节、明确标为当前实现的赛事工具与目标程序是现有接口；其余未实现入口仍是设计提案**。路径均相对本仓库；部署地址、账号与凭据由配置提供。

合并说明：本页保留 dev-aure 的赛事接口设计访谈，不是当前全部运行能力清单。合并后的配置入口、原生模型反馈、日常技能、单模型请求门禁及本地安全运行时见 [README.md](../README.md)、[DAILY_SURVIVAL.md](DAILY_SURVIVAL.md) 和 [TESTING.md](TESTING.md)。这些实现不代表完整阶段 skill 加载、compact/handoff、浮水/搭桥执行器等未来提案已经落地。

## 1. 目标与术语

目标：封装 mc-agent-api，为程序和小模型提供一致能力。harness 根据比赛清单与实际进度分阶段给目标；模型自主制定具体计划，程序管理执行约束、互斥、中断、恢复和导航。模型避免高频采样、数学运算与安全监控。

首要质量目标：长阶段任务可靠运行，模型对话或进程重启后能够核验并恢复进度。

- **任务**：持续目标及阶段进度。
- **动作**：一次可能改变游戏的底层调用。
- **观测**：带时间、会话和维度的状态事实。
- **工具**：模型可见入口，不等同于所有程序方法。
- **恢复任务**：重新观察并规划剩余工作，不承诺原动作断点续传。

## 2. 当前实现与已存在的程序接口

本节按拉取后的源码核对，替换旧 dev-aure 历史快照。`main.py` 使用 `core/survival_harness.py`，不是旧 `agent/loop.py`；完整链路见 [ARCHITECTURE.md](ARCHITECTURE.md)。

| 能力 | 当前代码与真实入口 | 状态/返回契约 |
|---|---|---|
| 配置 | `core.config.load_config(...)`；`main.py --check-config` | YAML/环境/CLI 已接通；不自动读 `.env` |
| 游戏客户端 | `MinecraftAgentClient(...)`；`get_status()`、`inventory()` | 返回 `ok/result` 封装；协议错误抛 `APIError`，`MinecraftAPIError` 保留兼容名 |
| 观察列表 | `get_blocks(...)`、`get_entities(...)`、`find_blocks(...)` | 返回校验后的列表；无类型方块查询是最近每类摘要，不是完整地图 |
| 移动 | `client.move_to(x,y,z,timeout_ms=8000,range=2)` | Python 参数仍叫 timeout_ms，已转换为 HTTP `timeout:8000`；单位毫秒，不再是旧 bug |
| 旧兼容方法 | `look_at(x,y,z)`、`get_inventory()` | 前者走 `/look`，后者与 inventory 一样返回 envelope；不可按旧裸 result 使用 |
| 生存调度 | `SurvivalHarness.tick(automatic=True)`、`snapshot(status=None)` | 已实现单动作线程、模型 worker、安全检查；快照含阶段/暂停/任务板等 |
| 调试控制 | `pause(reason)`、`resume()`、`request_step()`、`request_sleep()`、`close()` | 生存内部/白名单调试入口，不是新增赛事模型工具；resume 不重放 pending |
| 模型选择 | `LLMSurvivalPlanner.choose(observation,context,cancel_event)` | 返回一个 Decision；`observe_result(decision,result,observation)` 回传配对原生工具结果 |
| 目标程序 | `run_survival_task({task,resource?,item?,count?,radius?})`、`AutonomousTaskController` | daily/competition 的 12 类有界目标；每子步 fresh、复用现有技能和 pending，原目标只产生一次配对反馈，见 [AUTONOMOUS_TASKS.md](AUTONOMOUS_TASKS.md) |
| 模型门禁 | `ModelRequestGate.request(cancel_event)`、`snapshot()` | 本机线程/进程串行化；传输未知持久化锁定；不自动清锁 |
| 进度/记忆 | `SurvivalJournal.update/record`、`SurvivalMemory.recall/record_result` | 按 API/账号/world_id 隔离的检查点及有界 SQLite；competition 子状态可恢复终态，pending 不重放 |
| 模型工具目录 | `tools_for_objective("starter"/"daily"/"competition")` | 9/35/40 个已注册工具；competition 的 5 个任务工具由本地任务管理器执行，游戏工具仍受后验门禁 |
| 赛事任务 | `CompetitionTasks`、`tools_for_objective("competition")`、`MinecraftAgentClient.advancements()` | 规则目录、目标选择、progress/finish/force-exit/end、服务器 advancement 计分和终态检查点已实现；模型自报不算证据 |
| 要塞 | `EyeObservationCollector.capture()`、`CalculateStrongholdSkill.execute(...)` | 独立 Python/CLI；STRONGHOLD_TOOL_DEFINITION 未加入 starter/daily/competition 工具目录 |
| Markdown skill | `skills/stronghold_localization/SKILL.md` | 策略文档，无运行时按需加载器 |
| 扩展客户端 | `get_block([x,y,z])` / `get_block(x,y,z)`、`dig([x,y,z], collect=True)` / `dig(x,y,z,...)`、`get_inventory_counts()` | 坐标兼容与背包计数；主入口仍使用配置凭据。`move_to` 可省略 y，z 通过显式参数传入。 |
| 独立宏技能 | `NetherPortalSpeedrunSkill.execute(step=1, **kwargs)`、`CraftItemSkill.execute(...)`、`ChopTreeSkill.execute(...)` | Python 实现已合并，未绑定 starter/daily/competition；与模型 `craft_item` 的生存实现不同。 |
| 开发雷达 | `main.py --debug --radar`、`ContinuousRadarSkill.enable/disable/execute` | 后台读取并写开发 `MemoryEngine`；不用于 advancement 核验。 |

`PROCEDURAL_TOOL_DEFINITIONS` 中的 pillar_up/chop_tree/radar_sweep/cast_nether_portal 只是独立 schema，不由 `tools_for_objective(...)` 返回。特别是 `cast_nether_portal(axis)` 与分步技能需要的阶段坐标不是同一调用契约，不能直接注册到 harness。门户 `PortalTelemetryRecorder` 主动读取 API，不是现有监控服务的事件订阅或健康告警。


目标与能力差异：daily 的 13 项证据任务板不等于赛事 advancement 清单；competition 现在提供 advancement 任务板和结束 gate，但尚未实现完整阶段清单、动态 skill 加载或导航执行器；starter 的 `goal_complete` 与 competition 的 `end_competition` 是两套退出语义。

模型与动作两个恢复入口必须分清：`--resume` 只处理游戏 pending；`--acknowledge-model-idle` 只在已确认 provider 空闲后解除模型不确定锁。不能用重启/清文件绕过门禁；当前脚本退出会登出游戏，不是 handoff 保持在线功能。

### 当前目标程序与未来阶段调度的区别

`core/autonomous_tasks.py` 是实际执行目标的确定性程序，`start/next_action/record_result/interrupt/snapshot/state` 由 `AutonomyRuntime` 接入主 harness。它不自行发模型或游戏请求，也不保存可重放的写动作队列。已知失败有界换候选，紧急安全优先；未知子动作立即冻结，显式恢复必须在完整新观察下放弃未确认目标。全局运行预算与每目标预算共同约束执行。

这已经允许一个模型工具调用驱动多步材料、制作和设施工作，但不等于完整赛事阶段执行器、886 配方通用求解器、Markdown skill 加载、语义 compact/handoff 或下界/末地自动行动。默认目录为 9/35/40；关闭 autonomy 时模型请求中的 daily/competition 目录移除 `run_survival_task`，回到 34/39。

9/35/40 是完整注册目录。autonomy 正常阶段实际只向模型暴露目标与必要安全/赛事管理工具；任务 `needs_ai` 阶段开放原子修复，解析仍只接受本轮允许的工具。该过滤属于运行时工具路由，不是只在 prompt 中建议目标优先。

## 3. 底层 API 摘要

来源：外部 mc-agent-api 的 `docs/API.md` 和源码。具体部署应重新核对版本。路径相对配置的 `/api/v1`，不是新建 harness HTTP 服务。

| 能力 | 调用 | 关键语义 |
|---|---|---|
| 登录 | `POST /auth/login {"username":"…","password":"…"}` | token 在响应顶层；自动连接，等 ready 后操作 |
| 观察 | `GET /status`、`GET /inventory` | 不占普通动作锁 |
| 地形 | `POST /find_blocks {"types":["grass_block"],"radius":16,"limit":8}` | 指定 types 返回数组；省略时为种类摘要 |
| 实体 | `POST /get_entities {"types":["eye_of_ender"],"radius":64}` | 校验身份与坐标 |
| 事件 | `GET /events?since=0&limit=50` | seq 游标，缓冲有界 |
| 移动 | `POST /move_to {"x":10,"z":20,"range":2,"timeout":8000}` | timeout 毫秒；省略 y 为水平目标；阻塞占锁 |
| 用物品 | `POST /use_item {"item":"ender_eye"}` | 有消耗，超时不能盲目重发 |
| 停止 | `POST /stop {}` | 独立控制通道；需确认旧动作结束 |
| 姿态 | `POST /posture {"jump":true}` | 全量设置；未指定姿态关闭；持久状态可能被动作重放 |
| 短时控制 | `POST /control {"forward":true,"jump":true,"ms":300}` | 50～3000ms，占普通动作锁 |
| 看向目标 | `POST /look {"x":10,"y":64,"z":20}` | 不占锁但改变视线，需协调写入 |

约束：同机器人普通动作互斥，冲突返回 busy；观察可并发。业务失败可能 HTTP 200，必须检查 ok/error。登录、登出不一定有 result，解析应按端点区分。重复登录同名角色使旧 token 失效，应复用会话。

11月13日旧部署实测无 velocity；11月14日官方1.3.7（`1cfecc3`）已实际返回 speed/velocity，并进入当前模型快照和监控，见 [TESTING.md](TESTING.md)。处理缺失/null，不能把单次零速度当作卡住或到达的唯一证据，也不能把一次部署结果推广到所有服务版本。

## 4. 模块边界提案

本节至第 9 节描述未来赛事分工与已确认方向。除特别说明外，不是当前 daily 调用图；已有对应能力及差异见第 2 节。

```text
赛事清单与阶段控制器
  → 模型规划层：当前阶段目标与简短进度卡
  → 规则/阶段护栏 + 工具注册表：schema、handler、结果摘要
  → 任务运行时：目标、阶段、检查点、完成判定
  → 动作协调器：所有权、串行派发、取消、安全监控
  → 游戏网关：认证、协议适配、类型化响应与错误
  → mc-agent-api
```

建议：同一能力注册信息生成 schema，避免实现与 skill 漂移。所有执行路径，包括调试和恢复，都经过动作协调器。读取独立并行；stop 独立通道；posture/look 虽不占服务器锁，也有受控资源所有权。网关不负责目标规划，导航策略负责路点、风险和进展。计划/进度卡保持简短，不要求模型逐个预批 API 动作；校验比赛规则、阶段范围、工具权限和明确安全护栏。

### 当前已实现的 competition 模型工具

| 工具 | 参数 | 语义 |
|---|---|---|
| `select_task(goal_id)` | 规则 ID | 仅选择 scored、未确认、未冷却且当前观察已解锁的 ready/needs_prerequisites 目标；进入单活动任务阶段。 |
| `update_progress(summary)` | 简短进度文本 | 只改进度卡，不是完成证据。 |
| `finish_task(reason)` | 原因 | 要求 fresh 的活动目标检查；只有服务端 advancement 显示 completed 且规则 eligibility 为 scored 才加分，然后回到选择。 |
| `force_exit_task(reason)` | 原因 | 结束当前目标并 cooldown，回到选择；不清游戏 pending 或模型 uncertain。 |
| `end_competition(reason)` | 原因 | 只在 selection 且 fresh 全目录检测后进入 terminal；未知目标留 unknown、不计分。 |

工具定义见 `agent/tools.py`，状态由 `core/competition_tasks.py` 保存至 scoped journal。`competition` 目录共 40 个工具（35 个日常工具 + 5 个生命周期工具）；规则目录 125 项，8 个 scored 目标有当前行动计划。新增怪物猎人只在当前战斗门禁允许攻击时提供下一步；击中或实体消失不是 advancement 完成证据。

`CompetitionTasks.model_view()` 是每轮模型输入：选择阶段只列 `selectable_goal_ids` 与对应简表；执行阶段以 `active_goal` 展开条件、所缺资源、下一步和退出条件。`snapshot()` 保留全目录计分与监控信息，不作为模型候选列表发送；精简投影不改变终态需要的全目录检测。

## 5. 程序入口草案

除上方已标明“当前已实现”的五个工具外，下表均是统一赛事接口的抽象草案；不要把未绑定名称注册给模型。正常退出须先对预设项目目标完成检测；检测结果齐全后模型可选择结束。force-exit 当前任务应 stop/核验后回任务选择，并保留未完成状态。

| 入口候选 | 输入 | 结果 |
|---|---|---|
| `observe()` | 范围/新鲜度要求 | 时间、会话、维度、状态和缺失字段 |
| `select_task(goal_id)` | 当前阶段可做目标 ID | 已实现；实际语义见上表 |
| `update_progress_card(task_id, summary)` | 目标、已确认进度、下一意图 | 未实现；当前仅有 `update_progress(summary)` |
| `get_task(task_id)` | 标识 | 未实现；模型通过上下文接收完整目录和当前进度卡 |
| `finish_task(task_id, reason, evidence)` | 标识、原因及检测证据 | 草案接口未实现；当前 `finish_task(reason)` 用最新服务器证据结束单任务 |
| `force_exit_task(task_id, reason)` | 标识及原因 | 草案接口未实现；当前 `force_exit_task(reason)` 结束活动目标并 cooldown |
| `execute_action(spec)` | 内部动作和截止时间 | action_id、已确认或不确定的执行结果 |

任务状态候选：queued/running/waiting/interrupted/succeeded/failed/cancelled。动作状态单独管理，需表达 stopping 与 outcome_unknown。预设目标检测状态候选：unchecked/completed/not_completed/blocked/unknown；“检测完毕”表示每个预设目标均有检测结果，不表示全部目标都已完成。

结果需区分成功、失败、中断、取消请求中、结果不确定。超时后可能已发生消耗，不可默认“失败且可重试”。

恢复协议候选：请求 stop → 核实旧动作结束；不能确认时保留 outcome_unknown/执行占用 → 获取新状态 → 核验副作用/世界/维度 → 继续、重规划或等待。不能仅凭 stop 响应释放所有权，不能用新任务覆盖未核清的旧任务。

## 6. 模型决策与导航职责（赛事设计，尚未完整实现）

模型自主制定目标内策略；harness 提供当前全部可做目标及需要前置的目标，不只提供程序预排的 top-N。每轮给模型简短进度卡：当前目标、已核验进度、下一意图、规则/安全硬约束。遇到阻塞或重复输出时停止当前计划，把真实状态交模型重新决定；不得静默重试。模型的路径是任务/策略方向，不需逐个预批 HTTP 动作。

导航由 harness 全权负责，模型只给目的地，不选择路线。安全可行时优先水面通行，水面且未潜没不计氧气消耗；继续监控潜没、氧气、血量与进展。游泳、绕行优先选择不耗材料的方案；虚空、岩浆等危险区允许按材料预算搭路。普通 `move_to` 不保证保持浮水，未来水面执行器不能仅依赖它；当前生存安全逻辑仍可能拒绝/中断水中生产，未采用本段优先策略。

规划器按已观测地形构建局部移动图，执行器按移动类型落实路点。可参考 Baritone 的移动可行性、时间/材料代价和执行校验，但无法直接接入其客户端控制与设置。公开 API 不提供避水配置，不得假造 `liquidCost` 参数。未知区域不能当作安全路径；材料不足或无可验证通路时报告 route_blocked。

预设目标检测器逐项给证据与状态。`CompetitionTasks` 已用 `/advancements` 的 fresh 响应完成该 gate：缺失目标仍记录 unknown，未知不计分；所有非根目标被检查后，`end_competition` 才能进入 terminal。模型也可 force-exit 当前目标并回任务选择。

## 7. 已确认方向与设计树

### 用户已确认

| ID | 决策 |
|---|---|
| D1 | 按比赛目标/预制清单行动；harness 给阶段目标与当前可做项目，模型按资源和环境自主排序、规划。无需细致逐 API 计划，只需持续掌握目标、进度和下一意图。 |
| D2 | 超出预期行为/计划不可行时停止并重新规划；目标条件满足后模型可决定结束。 |
| D3 | 预设目标全部完成检测后才开放正常退出；模型可结束或继续。可强制退出当前任务并回任务选择。检测完不等于全部成就已完成。 |
| D4 | 模型重复输出触发循环兜底；compact 并允许一次重规划，仍重复则 force-exit 当前任务回任务选择。 |
| D5 | 有副作用动作结果不明先核验，不盲重发；接受最小 checkpoint 作为进度丢失兜底。 |
| D6 | 正式比赛关闭聊天调试/人工游戏操作；停机排障和改 prompt 仅为最后手段。 |
| D7 | 当时需求为参考 Pi 和 handoff skill 设计 compact/handoff，combat 为 compact 的笔误。后续 2026-11-16 用户另行明确要求狩猎与战斗；当前新增范围见 [COMBAT_AND_HUNTING.md](COMBAT_AND_HUNTING.md)，不等同语义 compact 已实现。 |
| D8 | 上下文超限由 harness 自动 compact，模型不负责决定是否触发。 |
| D9 | 危险状态可自动打断动作，交给确定性 reflex/“下意识行为”处理；之后程序核验状态并续接任务意图。触发条件待定。 |
| D10 | 导航由 harness 完全管理；模型只给目的地。可行安全时优先水面路线；水面不潜没不计氧气预算，但继续监控状态。虚空/岩浆等危险区允许搭路。 |
| D11 | Reflex 可使用已有食物、盾等资源应急；核验安全后，原目标与前提仍成立时自动续接任务意图，不重放被中断请求。 |
| D12 | 导航参考 Baritone；普通 move_to 不保持浮水，水面需要独立执行器。路线代价和控制时长保留为待测试校准参数。 |

### compact / handoff / checkpoint

- **compact**：harness 依据上下文预算自动触发；在同一对话内摘要旧内容，不重启模型对话或游戏会话。
- **handoff**：明确启动新模型对话时，由旧模型生成短交接，转移语义意图；不是自动超限机制，不代替 API 事实。
- **checkpoint**：少量机器可读恢复兜底，不建全量事件账本；恢复时从 API 重读世界状态。

### Pi / OMP 与 skill 分工参考

Pi/OMP 可参考：context budget+output reserve、模型调用边界自动压缩、摘要与最近完整 tool call/result 配对保留、压缩结果持久记录、overflow 有界恢复。BotSleep 是通用 Chat Completions，不能假设 provider 提供原生 compaction；具体实现参考见第 8 节“自动 compact 设计”。

Skill 只放小模型需要的触发条件、调用方式和退出要求；Pi 的 skill 渐进披露可作参考。可执行动作由独立 handler/tool 注册。本地 handoff skill 可借鉴“目标/进度/决定/风险/下一步”摘要格式，但不照搬其个人文件落点。

## 8. 自动 compact 设计

### 参考实现与本项目提案

参考已读取的 Pi [Compaction](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/compaction.md) 和 OMP [Compaction](https://github.com/can1357/oh-my-pi/blob/main/docs/compaction.md)。这些上游文档可能更新，落地前应记录所依赖的版本。

已确认的是“harness 自动触发语义 compact”；当前运行时仅有观察摘要、历史轮数和请求字节预算裁剪，并非本节机制。`LLM_HISTORY_COMPACTED` 日志代表丢弃旧完整事务，不代表模型生成了新摘要；底层请求 gate 同样必须覆盖未来 compact 的模型请求。以下方案尚未编码或冻结数值：

1. **预算**：在下一次模型调用前估算完整请求上下文，包含系统指令、当前 skill、工具 schema、摘要、保留消息和新增工具结果。配置实际 `context_window`、`reserve_tokens` 与近期保留预算；不能把 default 模型别名或通用固定值当作窗口大小。
2. **触发**：预计输入接近 `context_window - reserve_tokens` 时自动压缩。优先使用可用的服务 usage，并估算其后新增内容；缺 usage 时本地估算。不把累计计费 token 或旧请求 usage 当成当前请求准确长度。
3. **边界**：通常在工具结果已追加、下一次 assistant 请求前检查。保持完整的 tool_call/tool_result 配对，不能从工具结果中间切断。当前阶段/任务 ID 不变，不为 compact 顺带发送游戏 stop 或 logout。
4. **内容**：摘要只保留目标、已核验进度、关键决定/失败、下一意图、约束和 outcome_unknown；程序固定保留当前任务标识、规则/阶段要求，并按预算保留近期完整消息。压缩不靠硬删最早 N 条代替摘要。
5. **落盘与失败**：先生成/校验摘要，成功后原子提交摘要与保留边界，再替换模型输入。失败不能先删除旧历史；保留上一版和 checkpoint，采用有界恢复，防止无限压缩或新动作重复派发。边界与结果可落盘不等于要求保存全量事件账本。
6. **溢出兜底**：参考上游的 overflow 后压缩再尝试模型请求；只能有界重试模型请求，不能重放已执行游戏工具。若只有一次完整工具结果就超预算，先做结果裁剪/外存引用，不能误删任务约束。

Pi 的实现使用可配置 reserve/keepRecent 预算、摘要和保留消息边界重建上下文，并避免拆开工具调用与结果；OMP 同样持久化压缩条目和保留边界，支持不同 provider 的机制。BotSleep 使用通用 Chat Completions 时不能假设原生 compaction 端点可用。仅借鉴文本摘要路径和生命周期，不照搬编码 agent 的大窗口默认值、文件操作记录或完整会话树。

## 9. 导航分层与本轮访谈收敛

### Baritone 参考核查

核对了 Baritone `1.21.4` 分支的 [Settings.java](https://github.com/cabaletta/baritone/blob/1.21.4/src/api/java/baritone/api/Settings.java) 和 [CalculationContext.java](https://github.com/cabaletta/baritone/blob/1.21.4/src/main/java/baritone/pathing/movement/CalculationContext.java)：

- `allowSwimming` 控制主动游泳，所读源码默认为 false；主动游泳依赖疾跑许可及足够饥饿，不能据此假定全程水面或不耗氧。
- 水域通行代价会考虑移动速度修正，主动游泳可用时比较游泳速度；可借鉴为本项目的移动模式/可行性/成本模型。
- 上游能读取游戏世界并直接控制客户端；BotSleep 使用局部 API 观察，不能照搬其完整寻路或设置。只借鉴设计，不依赖不存在的 HTTP 参数。

### 本项目导航接口方向

```text
模型/任务给目的地
  → navigator：局部地图、移动边、代价、路点与重规划
  → executor：walk（现有局部移动）/ surface_swim、bridge（提案）
  → 动作协调器：互斥、取消确认、reflex 抢占与恢复
  → 游戏 API
```

- **walk**：在已校验的陆路段可使用 API move_to，但须监控实际进展和状态，不能把 waypoint 终点安全等同于整段安全。
- **surface_swim**：使用持续 `posture(jump=true)` 与有界 `control` 方向脉冲，逐段读取位置/潜没/氧气/血量；浮水不能靠 move_to 保证。方向计算和所有参数由程序负责，模型不参与。该执行组合是待验证提案，不是已验收能力。
- **bridge**：虚空/岩浆等危险处，在确认支撑、放置范围和库存后构建有限通路；材料/时间预算和关键资源保留由程序策略限制。未实现执行器不对模型暴露为现有工具。
- **reflex**：安全事件可抢占；先停止/确认普通动作释放，再执行安全恢复。资源/世界/目标仍满足时按当前意图重规划；状态不明、死亡或条件变化时回模型决策，不盲重发。

地图缓存范围、规划搜索算法细节、路线代价权重、脉冲时长和安全阈值属于实现提案，需用确定性测试和实机校准；不再逐项作为产品方向追问。保持正的移动代价，不用负奖励制造路径循环。水面优先不意味着绕远无上限或忽略风险；具体权衡应通过可配置代价和验收场景评估。

### 本次拉取后的设计状态

本轮方向已汇总为：阶段清单与模型自主选目标；简短进度卡；harness 管动作、导航和 reflex；最小 checkpoint；compact 自动、handoff 显式；目标检查 gate、正常/强制退出；重复输出兜底。当前已落地规则目录、任务选择、服务器证据、得分、退出 gate、终态恢复和只读赛事面板；完整阶段清单、语义 compact、reflex、水面/搭路导航仍未实现。

本轮按请求实现规则计分任务的基础生命周期和只读状态面板；未实现能力仍需单独限定范围并验证，不把尚未验证的参数、网络重试或导航策略写成已有能力。比赛维护只能是最后手段，不能成为正常自动恢复链。

## 10. 实施缺口与验收候选

旧分支的 get_blocks/look_at 路径、move_to 的 timeout 参数、配置加载和错误归一化已由合并后的统一客户端处理；这些不再是新的待办。接下来应扩展可执行 scored 目标计划、阶段清单与导航/采眼整合；当前不把 unsupported 目标注册为可执行工具，也不添加空报成功的占位技能。

状态对照：已有离线回归覆盖单动作互斥、取消/安全反馈、模型串行门禁、pending 恢复和后验验证；不等于完整赛事导航/恢复验收。版本化生存/速通阶段建议已进入上下文与监控，缺少适配的阶段保持 blocked；它不替代可恢复的完整速通执行器。仍待补齐的主要是：动态 skill 加载、重复输出触发 compact 的兜底链、语义 compact/显式 handoff、受协调的要塞工具和水面/搭路导航。赛事目标检测、退出工具和基础监控已接入。模型服务未知状态当前需明确确认后才能恢复，不能承诺完全自主长期故障恢复。最新验证见 [TESTING.md](TESTING.md)。

维护：标明事实/提案/已确认；接口修改同步实现、schema、skill 与测试；不记机器路径、凭据、token 或原始大日志。

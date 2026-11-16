# BotSleep / Minecraft Agent Harness 交接

更新日期：2026-11-16（Asia/Shanghai）。路径均相对仓库根目录；跨机器需要自行配置部署地址、账号和凭据，不依赖原机器的进程或在线会话。提交、合并和远端同步结果以 Git 记录为准，不据本文推断已经 push。

**基线状态（`HEAD 38db5d5`，文档与测试隔离已提交；本轮阶段候选、夜间准备、恢复工具与本地执行优先改动未提交）**：赛事基础闭环已随 `783e152` 提交并合并，不再是未提交的 `c21d623` 工作树。最新合并另加入独立门户浇筑、门户遥测、随机岩浆靶场、配方合成、整树伐木和雷达。它们没有自动进入生存模型执行链；starter/daily/competition 仍为 9/33/38 个工具，125 项规则目录中仍只有 7 个明确行动计划。

### 当前交接：安静树冠的本地分段下降

本轮最终离线复核：1081项中1077通过、3项实服跳过、1项既有Windows SQLite清理ERROR；新增44项树冠回归全部通过。最终基线和限制见 [TESTING.md](docs/TESTING.md) 首节。

用户新运行仍停在 `posY=92`，本地结果为 `no_verified_local_recovery`、0 steps；身体高度叶片已清，附近地面脚部 Y 为88。旧恢复只用身体高度的 `clear_foliage` 准备，再交完整 `retreat_from` 搜索；历史 threat 方向与实心终点约束会漏掉部分安全下降机会，不能据已清叶片宣称脱困。

新增 `core/canopy_geometry.py` 的 `plan_canopy_step`：要求居中站在当前脚下干叶上，在四个正方向最多3格内找降低1～2格的已核验落脚点；下降口稍远时，本次可先水平1格朝该出口推进，不盲目 wander。最多清一格侧下方叶片，绝不挖自身支撑。daily collector 仅在 `canopy_blocked` 且无显式危险时补充采样，规划预算最多96次方块读/3秒。

`skills/canopy.py` 的 `CanopyRecoverySkill` 执行内部 Python 动作 `descend_canopy(pos, clear_pos, expected_name)`，由 harness 本地专用，不新增模型工具。串行执行并先写 pending，每次写前重读12格实体门禁及支撑/身体净空；最多一次 `dig(collect=False)` 加一次 `move_to(range=0)`，精确确认落地。dig 结果未知时禁止 move 和重放，保留 pending。

新路径只用于无近敌的安静树冠恢复，忽略历史威胁方向；近敌时保留旧 retreat 逻辑。落到实心地面才清除恢复意图，落在叶片则重新观察继续规划。该内部动作的侧下方清叶权限与 `clear_foliage` 的身体高度限制不同，通用 clear/force 和 `force_clear_foliage` 范围不变；模型工具仍为9/33/38。不承诺任意树冠成功，四格直落仍拒绝。

离线覆盖包括模拟 `92→91→90→88` 到 ground、零模型请求、competition selection 先恢复、未知 pending、内部动作非模型工具、规划预算与失败目标排除。最终离线结果见 [TESTING.md](docs/TESTING.md) 首节，尚未实服验收。侧叶用例不是实服验收，不承诺任意树冠成功。本轮未重启或操控在线机器人，现场核查仅读日志和监控；当前进程需重启才能加载修改，尚不能宣称在线已脱困。

### 此前交接：harness 本地执行优先

此前本地执行链离线复核：1037项中1033通过、3项实服跳过、1项既有Windows SQLite清理ERROR；新增83项回归全部通过。空背包starter链32步、零模型请求。详细范围与限制见 [TESTING.md](docs/TESTING.md)。

新增 `core/local_execution.py`、`core/local_tasks.py`、`core/local_recovery.py`，接入 `core/survival_harness.py` 的模型路径：本地安全优先，任务与树冠脱困逐步尝试，明确失败或无进展后本地替代方案耗尽、无支持计划或预算耗尽才交模型。配置 `survival.local_execution` 默认 enabled=true、max_steps=64、max_seconds=180（秒），上限128步/600秒。每步重新观察、沿用原安全门禁/pending/串行真实技能；只保存预算与证据，不重放动作队列。

已登记且新鲜核验仍在加工的熔炉任务由本地等待与重观测推进；等待仍消耗本地步骤/时间预算，槽位未知、输出不匹配或预算耗尽才交模型。

失败位置写内部 `context.local_execution.failed_targets`，下一次规划合入受阻目标，先换其他已观察资源目标；未知写动作仍先锁定，不因换目标绕过。失败后换已观察资源目标的离线回归通过，零模型请求。

`main.py` 使用 `SurvivalHarness.next_poll_seconds`：busy/无退避本地ready最多50ms tick，本地成功中间步骤不等普通poll；失败退避与暂停保持原策略。聊天按 `next_debug_poll` 和原配置间隔独立节流，不放大聊天API请求；快tick不是20Hz实时反射。

接管摘要为 `context.local_execution`，含 phase/reason/steps/recent（最近最多6条动作结果）；动作未知进入 blocked_unknown，保留 pending 禁重放。模型槽未知或请求在途不本地生产，provider 出错不回退规则采集。身体高度清叶候选仍由本地和模型恢复共享；安静树冠另有本地专用分段下降，侧下方清叶不提供给模型。每步后重新观察并核验下降。7个比赛行动计划与9/33/38工具目录未增加，水面导航、自动搭路和完整赛事执行仍未实现。

运行摘要可查 `snapshot.local_execution`，审计事件为 `LOCAL_EXECUTION_STEP` / `LOCAL_EXECUTION_RESULT` / `MODEL_TAKEOVER`；`EventMonitor.local_execution` 投影与 HTML 卡片展示阶段、步数、原因、最多6条近期动作，当前决策按 `DECISION.data.source` 展示来源；缺失显示未确认。完整 starter 链路离线验证通过，覆盖采集扣材料、放置工作台、三件石工具与熔炉、goal_complete，零模型请求。本轮最终结果以 [TESTING.md](docs/TESTING.md) 首节为准；未做实服验收。下文的测试数字和现场记录均为此前记录，不作为本轮新增链路验收。

2026-11-16 实测日志出现 `llm_context_limit`：第一轮就达约 51–53KB，超过本地48KB，未发模型请求。旧测试只覆盖稀疏观察和空目录状态，未覆盖真实输入叠加。按用户新要求已改为状态解锁候选摘要、选中后单任务详情；没有抬高预算、删规则或只给top-N。密集观察和中文记忆的8轮离线回归均小于48KB且保留最近配对工具反馈；不代表实服已经恢复。阶段候选修复时全量为855项（历史），后续夜间修复的874项结果见下方和 [TESTING.md](docs/TESTING.md)。本轮不启动/重启机器人，不发模型请求，不清未知状态或改相邻API。

### 本轮补充：无床夜间开局不再只等天亮

实测会话曾成功选择石器任务并提出采5根木头，但模型返回后已跨过11000tick，被 night gate 拒绝，随后原地等待。已加 `night_preparation`：daily/competition 在满血、food>=12、无雷暴/近敌与完整观察/落地条件下，让模型做3格内单步准备；批量被拒后反馈给模型改选单步，床/已验证返家和危险抢占仍优先。模型/动作在途受伤、敌人进入至少12格缓冲或天气恶化会中断；pending和模型未知锁不变。清叶技能仍要求白天，不放进夜间候选。

新增纯策略、collector/harness/实际技能的本机假HTTP回归覆盖夜间真实库存/方块变化、跨黄昏重规划、取消旧提案与规则模式防绕过。该轮全量874项/43.333秒：870通过、3项实服跳过、1项旧SQLite清理ERROR；新增19项回归通过。仅离线验证，未热更新/重启用户机器人，未实际采集、调用模型、改时间或操作RCON；实服需加载新代码后独立核验，不能声称在线已修复。最新测试见 [TESTING.md](docs/TESTING.md)。

### 本轮补充：required、受阻报告与可控强制尝试

用户2026-11-16明确同意三项分开实现，并允许强制尝试覆盖“没有明确危险”的保守规则（如夜晚采集）。已改有tools请求使用required，daily/competition加入report_blocked和force_attempt，注册目录为9/33/38；实际工具随安全/任务phase裁剪。普通夜间不开放的清叶/采集可由force显式尝试一格，真实target/schema/材料/支撑/未知状态/近敌关键低血/雷暴液体火等仍禁止绕过。相同失败force不盲重试；连续wait会转报告；反复同状态受阻可退出活动赛事任务并冷却，不算完成。

已补树冠blocked意图：无明确危险时完整观察，模型可报告或清一格相邻叶片，之后重读下降路线；撤退HTTP完成后最多1.25秒/6次读取确认落地，记录实际位移与原因，不重复move_to、不把浮空/重生当成功。仅离线验证，运行中的用户机器人未重启/热加载，不发新真实API/模型/RCON，不清锁或写其运行检查点。该轮最终917项/33.865秒：913通过、3项门户实服跳过、1项旧SQLite清理ERROR；43项新增覆盖。完整结果见 [TESTING.md](docs/TESTING.md)。

### 本轮补充：force参数与运行中日志网页

模型曾反复只传force action/reason、把坐标写在reason，导致missing keys却收到笼统反馈。已给schema按真实action声明必填字段，错误明确缺pos/expected_name和JSON形状；不猜参数，连续格式错误转report。新增只读 `debug.monitor_tail`：用本机18931补读flight日志，不重启机器人；主程序monitor开关已在私有配置打开供下次启动18930使用。跨机器需自己启动辅助进程，不复用本机PID或假定网页在线。页面source=log_tail、心跳缺失为未确认，绝不把旧日志当实时游戏证据。参数修复需重启旧机器人进程后才生效。

最终全量943项/49.342秒：939通过、3实服跳过、1旧SQLite清理ERROR；新增26项回归通过，109个Python AST与82个文档相对链接通过。仅启动只读辅助进程、测试本机HTTP，未发游戏/模型/RCON请求或改运行检查点；详见 [MONITORING.md](docs/MONITORING.md) 和 [TESTING.md](docs/TESTING.md)。

### 本轮补充：通用force仍返回空参，改为观察绑定恢复

最新会话已加载anyOf参数修复，但小模型改成反复force_attempt `{}` / 一次多call；仅详细报错不足以解卡。已在canopy恢复下提供临时force_clear_foliage（reason可选，`{}`有效），harness选一个当前可见相邻、非含水且不在冷却/失败历史中的叶片并绑定；真实执行前仍复核，原native alias/call_id与canonical force结果配对。实际只给绑定恢复/report/wait，selection也允许安全清障。并修复旧撤退搜索冷却在模型回应前到期导致清障提案被抢占的问题；危险门禁不变。

本地真实请求快照离线重放已确认新接口能选择另一格合法叶片，跳过原目标冷却；未发送模型或游戏请求。最终全量954项/37.415秒：950通过、3实服跳过、1旧SQLite清理ERROR；新增11项回归通过。HTTP增加parallel_tool_calls=false仍不信任provider一定遵守。完整合成执行链证据和最终回归统一见 [TESTING.md](docs/TESTING.md)，在线进程需重新加载，不能据离线回放宣称实际已脱困。

## 1. 当前入口、范围与历史现场

当前主入口为 `main.py`，支持 YAML、环境变量与命令行参数，默认 sleep，生存模式按 objective 分为 starter/daily/competition；`bot_sleep.py`、根 `client.py` 分别兼容转发至主入口和统一客户端。普通运行依赖已列于 `requirements.txt`，RCON 为可选开发依赖。不存在“修改 YAML 不生效、必须运行旧独立睡眠脚本”的启动前提。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe main.py --config config.yaml --check-config
.\.venv\Scripts\python.exe main.py --config config.yaml --mode survival --objective daily
.\.venv\Scripts\python.exe main.py --config config.yaml --mode survival --objective competition
```

先填写 API 地址、允许登录的账号及口令。`--check-config` 不连接游戏。`llm.enabled` 默认关闭；需要模型时填写服务配置或 `BOTSLEEP_LLM_BASE_URL`、`BOTSLEEP_LLM_MODEL`、`BOTSLEEP_LLM_API_KEY` 环境变量，不假设 `.env` 自动加载。模型与监控配置详见 [README.md](README.md)。

**历史现场（2026-11-14）**：原 dev-ink 运行在18:32的记录中已因 `death_limit` 安全暂停；16:26加载修复后，16:35仍处于雷暴/近敌下的本地避险阶段。这些记录不代表当前进程或连接状态，也不是合并后新增实服验收。恢复前须按实际部署重新观察，独立处理动作 pending 与模型未知请求。

`.test-runtime/`、`.survival-test-runtime/` 和 `logs/` 均为本地忽略目录，不随 Git 拉取分发。接收端不能假定历史现场报告存在；需要时获取脱敏副本或另行验证，不根据历史记录启动第二个同名机器人。

### 最新合并能力的接线边界

| 能力 | 当前状态 |
| --- | --- |
| `NetherPortalSpeedrunSkill` | `build_scaffolding`、`execute(step=1, **kwargs)` 与 3 个分步方法已存在；需要显式阶段坐标，不是 `cast_nether_portal(axis)` 的完整实现。未接入模型任务和安全恢复。 |
| `PortalTelemetryRecorder` / `LavaLakeGenerator` | 前者主动读游戏并导出 `logs/telemetry/` JSON；后者通过 RCON 改测试地形。都不是只读系统监控或赛事完成证据。 |
| `RecipeBook` / `CraftItemSkill` / `ChopTreeSkill` | 886 个产物的配方目录及独立宏技能已合并；模型 `craft_item` 仍使用生存技能。聊天处理器有注入槽，但 `main.py` 没有注入合成/伐木对象。 |
| `ContinuousRadarSkill` / `MemoryEngine` | debug 加 `--radar` 时创建后台扫描；默认写 `data/game_logic/`，不是 scoped SQLite 或生存检查点。调试后检查受版本控制文件的 diff。 |
| 实服脚本 | 门户 unittest 默认跳过 3 项；合成/随机岩浆是 `main()` 脚本，无离线 unittest 用例。均需显式启用、专用世界及环境凭据，不能以脚本存在推断实服验证通过。 |

## 2. 已实现链路与验证边界

`survival.objective: starter` 执行初期石器工具链；`daily` 持续计算13项基于当前证据的任务，包含食物、材料、铁器、熔炼、床与住所、储物、死亡掉落恢复及维护。日常模式已注册31个工具，仍受步骤、时间、死亡及安全预算限制，不承诺无限无人值守。详情见 [DAILY_SURVIVAL.md](docs/DAILY_SURVIVAL.md)。

`competition` 需要模型规划，已接入规则版本、完整规则目录、条件 override、任务选择/进度/完成/强退/正常结束、服务器 advancement 计分与终态检查点。模型选择时只收到按当前新鲜资源/环境解锁的简化候选，选中后展开当前任务完整说明；全目录保留给本地检测和只读监控；只有 7 个早期目标具备明确行动计划。详细监控信号及缺口见 [MONITORING.md](docs/MONITORING.md)。

| 模块 | 当前状态 |
| --- | --- |
| `core/client.py`、`core/config.py` | 统一配置与协议；新增 get_block/dig 坐标三元组兼容、可省略 y 的 move_to 和 get_inventory_counts。独立程序仍应显式传入凭据。 |
| `core/survival_harness.py` | 单动作执行、独立单模型 worker、安全抢占、失败冷却、检查点及分 objective 预算；不是旧 `agent/loop.py` 骨架 |
| `core/competition_rules.py`、`core/competition_tasks.py` | 规则核对、任务状态、server advancement 计分、全目录终止 gate 与 terminal restore；非计分来源不授权得分 |
| `agent/llm_planner.py`、`agent/tools.py` | 原生 tool call/result 配对、参数验证、有限请求预算；starter 9项、daily 33项、competition 38项工具 |
| `core/model_gate.py` | 同服务及凭据作用域的跨线程/进程请求串行门禁，未知请求持久化锁定 |
| `core/daily_observation.py`、`core/daily_policy.py`、`core/survival_memory.py` | 新鲜证据、本地安全决策、按账号/世界隔离的 SQLite 记忆；记忆不是库存真值 |
| 睡眠与生存技能 | 已接入真实动作及后验验证；搭高、落地水和20Hz反射仍未接入 |
| `integrations/ninjabrain/`、`skills/stronghold.py` | 独立采样与官方计算核心；未纳入日常模型工具目录 |
| `skills/stronghold_localization/SKILL.md` | 小模型策略文档，尚未自动加载到运行上下文 |

**历史合并验证**曾记录 772 项通过（17.137 秒，0 跳过），包含原 dev-ink 的740项基线、31项要塞回归及1项目录隔离回归；当时固定 Ninjabrain Bot 1.5.2 JAR 通过 SHA-256 校验，compileall、示例配置检查和监控 JavaScript 语法检查通过。合并前740项/15.126秒同为历史基线。上述数字均属历史记录；本轮最终结果以 [TESTING.md](docs/TESTING.md) 为准，不用历史通过覆盖其他环境中的错误。

历史真实自然地图记录：2026-11-14 15:50–15:56的同一会话完成清叶、两次采木、木板/木棍/木镐制作，11个动作的后续原生工具反馈配对正确；工作台缺料、移动失败后模型改选行动。16:03被僵尸击杀并空背包重生。不得把死亡前物品当作当前持有，也不能用此记录证明后续新会话或完整日常链通过。睡眠与确定性石器靶场另有历史验证，完整食物、熔炼、建屋、储物及长期生存仍待验收，证据见 [TESTING.md](docs/TESTING.md)。

API 1.3.7（官方 `1cfecc3`）已实际返回 `speed/velocity`，速度进入模型与监控；缺失/null表示未知，不是零。`dig_unconfirmed`、树冠分段撤退等有离线回归，但不能把离线检查当作真实恢复成功。原模型服务路由为 `default`，历史响应自报 `qwen3.6-35b-a3b`；路由或模型列表不保证实际后端身份，当前响应缺失时应显示“未确认”。

## 3. 协作、安全与小模型约定

- 用户要求仅修改 BotSleep，不修改或提交相邻 `mc-agent-api`；历史 API 实验补丁已撤回。不要为验收给物、传送、改时间/天气或破坏用户普通世界。
- 模型服务不支持并发，不得在机器人请求在途时另发探测。明确完整 HTTP 429/5xx 可按预算退避；传输超时或断连保留 `llm_request_uncertain`，确认远端结束前不清锁、不自动重发。
- `--resume` / `#resume` 只授权重新观察并规划游戏任务，不重放未知动作，也不清模型请求槽。只有确认模型服务空闲才使用 `--acknowledge-model-idle`。`#stop` 请求取消并暂停，不能保证后端瞬时停止。
- 配置、凭据、原始模型日志、世界、运行台账、JAR与缓存不随仓库分发。私有日志路径仅提供证据索引，不代表接收端已经拥有这些文件。
- 面向游戏 AI 的 skill 放在本仓库，默认约300～600中文字、明确接口调用表与最多五步流程；公式、原理及长示例放 `references/`。未绑定接口明确标注，不虚构工具。
- harness 优先选择已支持任务的下一步并处理本地脱困，耗尽后由模型接管；会话、坐标转换、采样、数学计算与并发监控由 harness 承担。普通动作串行，未知副作用先核验，完成必须有证据。

[HARNESS_INTERFACES.md](docs/HARNESS_INTERFACES.md) 中 `select_task`、`update_progress`、`finish_task`、`force_exit_task`、`end_competition` 已作为 competition 工具实现；其余通用接口抽象、compact/handoff、自动 reflex、水面导航与搭路仍属提案。该基础闭环不等于完整赛事策略，也不保证 45 项 scored 目标都可执行。

## 4. Ninjabrain Bot 集成

详细使用与边界见 `docs/STRONGHOLD.md`；上游来源和 GPL v3 许可见 `third_party/ninjabrain/NOTICE.md`、`third_party/ninjabrain/LICENSE`。

- 固定官方 1.5.2 JAR，校验 SHA-256 后缓存于 `.cache/ninjabrain/`，通过 JSON 子进程调用无界面 Java 桥接。
- 直接使用官方 Posterior，保留生成环、生物群系偏移、角度误差和最近要塞约束；不启动 GUI、剪贴板或热键。
- 适用原版 Java 1.19+ 主世界，坐标观测绕过人工准星修正；不接入 divine、船眼或下界盲穿功能。
- `EyeObservationCollector(client).capture()` 消耗一只眼，按名字和 ID 跟踪一只新眼的多点轨迹，输出 `throw/quality/diagnostics`。
- `CalculateStrongholdSkill().execute(throws=[...], top_k=2)` 只计算，返回 `success/data/error_code`；候选在 `data.candidates`。`STRONGHOLD_TOOL_DEFINITION` 仅是独立 schema 常量，没有进入 starter/daily 工具目录或模型分派。
- CLI `capture` 会自行登录并在结束时登出；持续 harness 会话应使用注入 client 的 Python 入口，避免顶掉会话。

API 的 `use_item(ender_eye)` 会返回 `landed_at/eye_vector/bearing_deg`，但源码选择最近的新实体，可能误认；采样器不直接依赖该方位角。API 坐标舍入到 0.1 方块，方位角取整到 1°，因此应从坐标拟合方向。

## 5. 历史真实验证：2026-11-13 五眼定位

用户在测试服准备眼睛并移动假人后，使用普通游戏 API 完成 5 次投眼：每次 11～13 个不同水平位置，水平跨度约 12.1 方块，实体名实际为 `eye_of_ender`。

| 观测数 | 首选区块 | 当时模型概率 |
|---|---|---|
| 1 | `(-86,-74)` | 0.72% |
| 2 | 同上 | 53.20% |
| 3 | 同上 | 73.27% |
| 4 | 同上 | 88.21% |
| 5 | 同上 | 98.42% |

最终投眼目标 `x=-1376,z=-1184`，下挖参考 `x=-1372,z=-1180`；次选 `(-87,-73)` 约 1.58%。**这是原测试世界的推断，不能用于其他世界；地下 Y、入口实物和传送门房均未挖掘确认。**

消耗 5 只眼（64→59）。寻路曾入河超时、沉水并降到 18 生命值，随后通过正常上浮/游泳恢复到 20。当时最终记录为地面空闲；跨机器不得假设原会话仍存在。全过程未用 RCON、管理员定位、存档或挖掘。

原始证据文件名：`logs/live_test_result.json`、`logs/probe_result.json`、`logs/eyes-local-test-2026-11-13.json`、`logs/stronghold_live_result.json`、`logs/stronghold_live_prediction.json`。**logs 被忽略，不随正常 Git 拉取分发**；缺文件时向产出者获取脱敏副本，或重新采样，不能把路径当作已有文件。

## 6. 误差、运动方案与精简 skill

用户指出投眼数偏多。复查发现当前采样器把 `atan((0.1+最大残差)/跨度)` 作为标准差，偏保守，未充分利用多点信息量，也包含悬停段抖动。

离线截取外飞阶段、去重并作 TLS/PCA，估计量化/拟合统计项约 0.107～0.142°；示例叠加 0.1° 系统底限后约 0.146～0.174°，低于原 0.606～0.743°。前两眼回放即可强烈区分相邻候选。**这是误差模型敏感性分析，不是准确率标定；无独立真值，不能因后验更尖锐就认定新 σ 正确。**

策略已整理至 `skills/stronghold_localization/SKILL.md`，从 1664 压缩到 927 字符；研究在其 `references/research.md`：

- 有效飞行段采样，数学与 σ 由程序处理，不让模型手算。
- 近距离以两眼为目标，优先比较 20～50 格安全横移；有歧义才补眼。
- 用候选角分离和地形成本选点，不固定长距离横移。
- 短路点、监控卡住/受伤/潜没、明确停止和上浮恢复。
- 结果区分入口参考与传送门房，明确误差假设。

**运行代码尚未采用新的统计 σ、提前结束采样、自动选点或统一导航监控；skill 只是策略文档。** 旧文档中的 100～300 格横移建议不是新策略默认值。临时 Codex 个人 skill 空模板已删除，仓库 skill 才是交付物。后续编写约定已记录到 `AGENTS.md`。

## 7. 后续工作与模型接口历史

2026-11-13 曾以模拟工具验证 Chat Completions 的工具调用与工具结果往返；这是协议验证，不能算真实要塞自主任务。后续日常模型主循环已经接通，早期“无 YAML 启动、工具结果未回传、#stop仅提示”的缺口不再是当前入口状态。

仍需分开验证：自然地图持续食物/熔炼/住所/储物闭环；树冠和水域安全导航；要塞采样的统计误差与独立真值；要塞技能未来模型绑定、自动选点与导航整合。纯计算概率不是找到传送门房的证据，不注册尚未实现或未验证的执行器。

## 8. 跨机器准备与文件索引

建议 Python 3.11（生存入口要求3.10+）；创建本机虚拟环境并安装 `requirements.txt`。计算桥接另需 JDK 11+，保证 `java`/`javac` 可发现。不要复制原机器虚拟环境或已编译缓存。

```powershell
python -m pip install -r requirements.txt
python -m integrations.ninjabrain setup
python -m integrations.ninjabrain calculate --input data/examples/stronghold_throws.json
python -B -m unittest discover -s tests -v
```

`setup` 下载固定官方1.5.2 JAR并检查 SHA-256，后续计算可离线；`.cache/ninjabrain/` 不提交。缺少引擎时测试会明确跳过，不能报告为引擎全部通过。前轮合成示例 CLI 曾计算首选区块 `[128,0]`，与实际世界无关；本轮状态按 TESTING 首节记录。

`capture` 会登录、消耗末影之眼并在结束时登出，可能替换同名旧会话。需要实机采样时按 [STRONGHOLD.md](docs/STRONGHOLD.md) 使用自己的部署配置；持续 harness 进程应使用受协调的注入 client 入口，不能另起同名 CLI 采样。禁止把独立采样器误当作已注册的日常模型动作。

| 路径 | 用途 |
| --- | --- |
| `README.md`、`AGENTS.md` | 当前启动、验证边界及贡献/skill约定 |
| `docs/DAILY_SURVIVAL.md`、`docs/SURVIVAL.md`、`docs/TESTING.md` | 日常与初期生存契约、实际验证记录 |
| `docs/HARNESS_INTERFACES.md` | 当前 competition 任务工具子集、已确认方向与未实现提案 |
| `integrations/ninjabrain/`、`skills/stronghold.py` | 独立官方计算桥接、采样器和CLI |
| `skills/stronghold_localization/SKILL.md`、其 `references/research.md` | 精简策略与误差研究，尚非运行时加载器 |
| `tests/test_stronghold.py`、`data/examples/stronghold_throws.json` | 要塞计算回归及合成输入 |
| `third_party/ninjabrain/` | 上游来源及GPL v3许可证 |
| `probe_api.py` | 早期探测脚本；不是当前主入口，运行前先核对其动作与局限 |

跨机器不复用历史PID、token、同名机器人会话或旧世界的投眼记录。游戏/API白名单、凭据、地址和存档由部署者配置；回环地址只表示当前执行机器。Git提交、远端同步和接收端完整性单独核验，本文不声称代码已推送或生产进程已加载合并结果。

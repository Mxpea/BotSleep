# BotSleep / Minecraft Agent Harness 交接

**2026-11-18 本地集成**：以 `main 9018f76` 为基线合入 `dev-aure 4e0a59e` 的非重复能力。树冠恢复仍走 `canopy_escape`，任务子步仍经 `AutonomousTaskController`。迁入赛事阶段投影、单工具且非并行的请求约束，以及只读日志补看。普通 `daily` 可设 `survival.autonomy.local_opening: true` 走同一任务控制器的确定性工具开局；默认关闭，不改变赛事任务选择。新增的卡树回归复用了双方共同关注的地形，没有启用第二套下降逻辑。

合并前的 `9018f76` 已在普通本地服走通原树冠位置到实心地面的恢复（7 次移动，Y=92→90→88，零模型请求）。该结果不是合并后模型与赛事链的实服验收；复核范围见 [TESTING.md](docs/TESTING.md)。仍在处理旧冲突的工作区不要直接覆盖，提交历史以 Git 为准。

**2026-11-17 合并更新**：本次将 `develop-tianya/shelter-workflow 39afec8` 合入 `main 02343f9`，在独立工作区处理冲突。住所选用共享 `StructureSpec` 四朝向蓝图，沿用主线逐步验收和自主目标调度；旋转后的床、箱子和入口同步处理。技能注册表与撤退控制器完成去重，同时保留主线战斗/桶类技能、树冠和觅食恢复、生命序号及串行模型门禁。新分支的门户、3D 世界、洞穴与分层采矿、容器为独立能力；没有自动接管日常地表行动。新增实例锁和只读 `botctl`，会话保持须显式启用。合并检查和未验收边界见 [TESTING.md](docs/TESTING.md) 顶部；下方 11 月 16 日状态属于历史。

更新日期：2026-11-16（Asia/Shanghai）。路径均相对仓库根目录；跨机器需要自行配置部署地址、账号和凭据，不依赖原机器的进程或在线会话。提交、合并和远端同步结果以 Git 记录为准，不据本文推断已经 push。

**当前状态**：`dev-ink` 以已同步主线的 `402cc50` 为本轮开发基线；官方 API 为 **1.3.14 / `37be500`**，未添加 API 本地源码补丁。主线的赛事基础闭环、独立门户浇筑、门户遥测、随机岩浆靶场、配方合成、整树伐木和雷达已纳入本分支。独立技能没有自动进入模型执行链；本轮加入短步战斗、取液体、受限岩浆狩猎后，starter/daily/competition 为 **9/35/40** 个工具，125 项规则目录中 **8** 个有明确行动计划。

本轮将猪/牛/羊/鸡狩猎改为近距有界挥击，加入骷髅/僵尸的有盾及无盾分支、弓弩单发、苦力怕退避与源液回收。执行失败反馈、交战预算、撤退要求及未知动作 pending 接入同一 harness；模型仍只允许一个在途请求。阶段建议通过 `survival.intent: survival|speedrun` 选择，并进入模型上下文与只读监控。详见 [COMBAT_AND_HUNTING.md](docs/COMBAT_AND_HUNTING.md)、[SURVIVAL_PLAYBOOK.md](docs/SURVIVAL_PLAYBOOK.md)；最新验证见 [TESTING.md](docs/TESTING.md)。后续验收只为私有配置增加 `survival.operator_goal`，未改变模型凭据、地址或路由。

**最新验证（2026-11-16 18:09–18:23）**：原普通世界一次真实模型请求返回木镐制作目标；程序在 12 个子步骤中采到 3 根原木、完成木板/木棍/工作台/木镐，两次放置失败由本地换位置恢复，目标内新增模型请求为 0，结束后原生反馈仅配对一次。返回模型为 `qwen3.6-35b-a3b`，请求路由 `default`。全量 1263 项中 1260 通过、3 实服专用跳过。后续圆石目标本地探索耗尽会升级询问；发现并修复了修复目录过大导致的请求字节超限，尚需后续实际修复请求验收。18:23 采样机器人满血满饥饿、持有木镐，夜间本地等待，服务器/API/监控继续运行。早期骷髅击杀和未完成树冠路线仍保留在测试记录中；不能把这次生产成功等同于可靠过夜。下方 15 点各段为历史现场。

**此前真实启动（2026-11-16 15:06 采样）**：按用户要求运行原普通存档的日常生存，无末影龙完成目标。游戏 `127.0.0.1:25585`、API `18920`、监控 `18930` 当时均在线；机器人在树冠 `[4,124,6.6]` 夜间等待/撤退复查，仍无安全下地路径，未验收本轮采集、制作或战斗。已修复模型混淆 terrain 汇总代表坐标和 blocks 候选的问题，idle 边界重启加载；完整离线 1007 项中 1004 通过、3 实服跳过。进程状态会变化，不应直接信本文或旧 PID。

**目标程序新增状态（源码与离线边界）**：daily/competition 默认增加 `run_survival_task`，目录为 9/35/40。模型选择一项有界目标后，程序逐步观察、选择原子技能、记录 pending 并验证结果，正常子步不再请求模型；终态/升级/未知或显式暂停边界对原工具调用反馈一次。已知局部失败可换候选，近敌/食物/睡眠由本地安全层抢占；未知写入不能自动重放。新程序的 12 类目标和常用配方子集未全部经过普通地图实服验收。

本轮开发交接时，主负责人已报告机器人安全登出、维护监控继续、游戏服与 API 保留；这是当时状态，不是当前进程存活保证。上方 15:06 记录属于此前实测快照，不得当作新目标程序已验收。恢复前核对私有配置、模型槽和游戏 pending，测试及真实恢复结果统一由 [TESTING.md](docs/TESTING.md) 记录。

15:36 恢复尝试中，模型仍选择原子 move/clear，说明仅在 prompt 写 prefer 不能确保长任务授权。为此增加实际动态目录：正常规划仅暴露目标与必要安全/赛事管理工具，`needs_ai` 后才开放原子修复。主负责人随后安全登出机器人、保留维护监控等待加载；这次原子动作选择不是新目标程序已完成的证据。

目标配置、状态恢复和监控计数解释见 [AUTONOMOUS_TASKS.md](docs/AUTONOMOUS_TASKS.md)。默认目标预算为 128 步/600 秒、4 次连续本地失败、8 次无进展、8 次局部探索；全局预算仍限制整个运行。`model_planning_rounds` 是模型规划 worker 启动次数，repair 可能增加 HTTP 请求；`model_calls_avoided` 是已授权目标下程序选择的子步骤数，不是节省费用或 HTTP 次数的测量值。

**历史采样（2026-11-16 15:50）**：新版机器人已于 15:44 恢复，原服务器/API/监控保持开启。全套 1090 项中 1087 通过、3 实服跳过。实服 54 个本地应急循环没有新增模型请求；曾被僵尸击杀并自然恢复，白天仍被树冠路线与附近骷髅阻碍。此轮尚未验证生产目标采集/制作成功。完整证据及检查点累计计数说明见 [TESTING.md](docs/TESTING.md)。

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

`survival.objective: starter` 执行初期石器工具链；`daily` 持续计算13项基于当前证据的任务，包含食物、材料、盾与桶、铁器、熔炼、床与住所、储物、死亡掉落恢复及维护。日常模式完整注册35个工具，实际模型请求按目标/修复阶段过滤，仍受步骤、时间、死亡及安全预算限制，不承诺无限无人值守。详情见 [DAILY_SURVIVAL.md](docs/DAILY_SURVIVAL.md)。

`competition` 需要模型规划，已接入规则版本、完整规则目录、条件 override、任务选择/进度/完成/强退/正常结束、服务器 advancement 计分与终态检查点。模型每轮可看到全部目录；8 个目标具备明确行动计划，新增怪物猎人只在当前具备可执行攻击条件时开放，计分仍只认服务端 advancement。详细监控信号及缺口见 [MONITORING.md](docs/MONITORING.md)。

| 模块 | 当前状态 |
| --- | --- |
| `core/client.py`、`core/config.py` | 统一配置与协议；新增 get_block/dig 坐标三元组兼容、可省略 y 的 move_to 和 get_inventory_counts。独立程序仍应显式传入凭据。 |
| `core/survival_harness.py` | 单动作执行、独立单模型 worker、安全抢占、失败冷却、检查点及分 objective 预算；不是旧 `agent/loop.py` 骨架 |
| `core/competition_rules.py`、`core/competition_tasks.py` | 规则核对、任务状态、server advancement 计分、全目录终止 gate 与 terminal restore；非计分来源不授权得分 |
| `agent/llm_planner.py`、`agent/tools.py` | 原生 tool call/result 配对、参数验证、有限请求预算；完整注册 starter 9 项、daily 35 项、competition 40 项，实际请求再按目标/修复阶段过滤 |
| `core/model_gate.py` | 同服务及凭据作用域的跨线程/进程请求串行门禁，未知请求持久化锁定 |
| `core/daily_observation.py`、`core/daily_policy.py`、`core/survival_memory.py` | 新鲜证据、本地安全决策、按账号/世界隔离的 SQLite 记忆；记忆不是库存真值 |
| 睡眠与生存技能 | 已接入真实动作及后验验证；搭高、落地水和20Hz反射仍未接入 |
| `integrations/ninjabrain/`、`skills/stronghold.py` | 独立采样与官方计算核心；未纳入日常模型工具目录 |
| `skills/stronghold_localization/SKILL.md` | 小模型策略文档，尚未自动加载到运行上下文 |

**历史合并验证**曾记录 772 项通过（17.137 秒，0 跳过），包含原 dev-ink 的740项基线、31项要塞回归及1项目录隔离回归；当时固定 Ninjabrain Bot 1.5.2 JAR 通过 SHA-256 校验，compileall、示例配置检查和监控 JavaScript 语法检查通过。合并前740项/15.126秒同为历史基线。当前以本文开头的本轮结果为准，跨环境结果需分别解读。

历史真实自然地图记录：2026-11-14 15:50–15:56的同一会话完成清叶、两次采木、木板/木棍/木镐制作，11个动作的后续原生工具反馈配对正确；工作台缺料、移动失败后模型改选行动。16:03被僵尸击杀并空背包重生。不得把死亡前物品当作当前持有，也不能用此记录证明后续新会话或完整日常链通过。睡眠与确定性石器靶场另有历史验证，完整食物、熔炼、建屋、储物及长期生存仍待验收，证据见 [TESTING.md](docs/TESTING.md)。

历史实测 API 1.3.7（官方 `1cfecc3`）返回 `speed/velocity`，速度进入模型与监控；缺失/null表示未知，不是零。当前官方 1.3.14 增加每 bot 独立 worker、非阻塞控制及扫描、重生就绪和挖掘/窗口行为修正；本轮仅做源码与离线兼容验证。`dig_unconfirmed`、树冠分段撤退等回归仍保留，不能把离线检查当作真实恢复成功。原模型服务路由为 `default`，历史响应自报 `qwen3.6-35b-a3b`；路由或模型列表不保证实际后端身份，当前响应缺失时应显示“未确认”。

## 3. 协作、安全与小模型约定

- 用户允许拉取 `mc-agent-api` 官方更新；源码适配只修改 BotSleep，不在相邻 API 添加或提交本地补丁。历史 API 实验补丁已撤回。不要为验收给物、传送、改时间/天气或破坏用户普通世界。
- 模型服务不支持并发，不得在机器人请求在途时另发探测。明确完整 HTTP 429/5xx 可按预算退避；传输超时或断连保留 `llm_request_uncertain`，确认远端结束前不清锁、不自动重发。
- `--resume` / `#resume` 只授权重新观察并规划游戏任务，不重放未知动作，也不清模型请求槽。只有确认模型服务空闲才使用 `--acknowledge-model-idle`。`#stop` 请求取消并暂停，不能保证后端瞬时停止。
- 配置、凭据、原始模型日志、世界、运行台账、JAR与缓存不随仓库分发。私有日志路径仅提供证据索引，不代表接收端已经拥有这些文件。
- 面向游戏 AI 的 skill 放在本仓库，默认约300～600中文字、明确接口调用表与最多五步流程；公式、原理及长示例放 `references/`。未绑定接口明确标注，不虚构工具。
- 模型选择目标和下一行动；会话、坐标转换、采样、数学计算与并发监控由 harness 承担。普通动作串行，未知副作用先核验，完成必须有证据。

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

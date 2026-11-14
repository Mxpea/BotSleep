# BotSleep / Minecraft Agent Harness 交接

更新日期：2026-11-14（Asia/Shanghai）。路径均相对仓库根目录；跨机器需要自行配置部署地址、账号和凭据，不依赖原机器的进程或在线会话。提交、合并和远端同步结果以 Git 记录为准，不据本文推断已经 push。

## 1. 当前入口、范围与现场状态

当前主入口为 `main.py`，支持 YAML、环境变量与命令行参数；`bot_sleep.py`、根 `client.py` 分别兼容转发至主入口和统一客户端。普通运行依赖已列于 `requirements.txt`，RCON 为可选开发依赖。不存在“修改 YAML 不生效、必须运行旧独立睡眠脚本”的启动前提。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe main.py --config config.yaml --check-config
.\.venv\Scripts\python.exe main.py --config config.yaml --mode survival --objective daily
```

先填写 API 地址、允许登录的账号及口令。`--check-config` 不连接游戏。`llm.enabled` 默认关闭；需要模型时填写服务配置或 `BOTSLEEP_LLM_BASE_URL`、`BOTSLEEP_LLM_MODEL`、`BOTSLEEP_LLM_API_KEY` 环境变量，不假设 `.env` 自动加载。模型与监控配置详见 [README.md](README.md)。

**2026-11-14 18:32 交接记录**：原运行目录的 dev-ink 机器人仍使用合并前已加载代码；PID40544、会话 `2a30267e20eb` 已运行270步，因 `death_limit` 安全暂停，重生后生命/饥饿20、位置 `[-2.5,125,10.5]`，监控 HTTP 200、模型槽 `idle`。这不是合并造成的停止；本次合并不重置死亡预算、不重启机器人，也不替换其运行目录。PID、位置及状态仅是此时间点的记录，后续必须重新观察。

16:26安全空闲时加载过对角清叶、几何改变后解除旧失败、紧急撤退短观察等修复；16:35仍是雷暴/近敌下的本地避险阶段，该状态现为历史。当前实时信息以私有 `.test-runtime/processes.json`、现场报告和只读监控时间戳为准；禁止根据历史 PID 或旧配置启动第二个同名机器人。

## 2. 已实现链路与验证边界

`survival.objective: starter` 执行初期石器工具链；`daily` 持续计算13项基于当前证据的任务，包含食物、材料、铁器、熔炼、床与住所、储物、死亡掉落恢复及维护。日常模式已注册31个工具，仍受步骤、时间、死亡及安全预算限制，不承诺无限无人值守。详情见 [DAILY_SURVIVAL.md](docs/DAILY_SURVIVAL.md)。

| 模块 | 当前状态 |
| --- | --- |
| `core/client.py`、`core/config.py` | 统一公开 API 封装、业务错误与未知结果区分、配置加载校验；旧路径和移动 timeout 参数已适配 |
| `core/survival_harness.py` | 单动作执行、独立单模型 worker、安全抢占、失败冷却、检查点及预算；不是旧 `agent/loop.py` 骨架 |
| `agent/llm_planner.py`、`agent/tools.py` | 原生 tool call/result 配对、参数验证、有限请求预算；starter 9项与 daily 31项可执行工具 |
| `core/model_gate.py` | 同服务及凭据作用域的跨线程/进程请求串行门禁，未知请求持久化锁定 |
| `core/daily_observation.py`、`core/daily_policy.py`、`core/survival_memory.py` | 新鲜证据、本地安全决策、按账号/世界隔离的 SQLite 记忆；记忆不是库存真值 |
| 睡眠与生存技能 | 已接入真实动作及后验验证；搭高、落地水和20Hz反射仍未接入 |
| `integrations/ninjabrain/`、`skills/stronghold.py` | 独立采样与官方计算核心；未纳入日常模型工具目录 |
| `skills/stronghold_localization/SKILL.md` | 小模型策略文档，尚未自动加载到运行上下文 |

合并后全套 **772项测试通过（17.137秒，0跳过）**：包含原 dev-ink 的740项基线、31项要塞回归及1项目录隔离回归。固定 Ninjabrain Bot 1.5.2 JAR 已下载并通过 SHA-256 校验，真实计算引擎用例没有跳过；compileall、示例配置检查和监控 JavaScript 语法检查通过。合并前 dev-ink 的 **740项/15.126秒** 仅保留为历史基线。

真实自然地图记录：15:50–15:56的会话 `b8b27ada1620` 完成清叶、两次采木、木板/木棍/木镐制作，11个动作的后续原生工具反馈配对正确；工作台缺料、移动失败后模型改选行动。16:03被僵尸击杀并空背包重生。不得把死亡前物品当作当前持有，也不能用此记录证明后续新会话或完整日常链通过。睡眠与确定性石器靶场另有历史验证，完整食物、熔炼、建屋、储物及长期生存仍待验收，证据见 [TESTING.md](docs/TESTING.md)。

API 1.3.7（官方 `1cfecc3`）已实际返回 `speed/velocity`，速度进入模型与监控；缺失/null表示未知，不是零。`dig_unconfirmed`、树冠分段撤退等有离线回归，但不能把离线检查当作真实恢复成功。原模型服务路由为 `default`，历史响应自报 `qwen3.6-35b-a3b`；路由或模型列表不保证实际后端身份，当前响应缺失时应显示“未确认”。

## 3. 协作、安全与小模型约定

- 用户要求仅修改 BotSleep，不修改或提交相邻 `mc-agent-api`；历史 API 实验补丁已撤回。不要为验收给物、传送、改时间/天气或破坏用户普通世界。
- 模型服务不支持并发，不得在机器人请求在途时另发探测。明确完整 HTTP 429/5xx 可按预算退避；传输超时或断连保留 `llm_request_uncertain`，确认远端结束前不清锁、不自动重发。
- `--resume` / `#resume` 只授权重新观察并规划游戏任务，不重放未知动作，也不清模型请求槽。只有确认模型服务空闲才使用 `--acknowledge-model-idle`。`#stop` 请求取消并暂停，不能保证后端瞬时停止。
- 配置、凭据、原始模型日志、世界、运行台账、JAR与缓存不随仓库分发。私有日志路径仅提供证据索引，不代表接收端已经拥有这些文件。
- 面向游戏 AI 的 skill 放在本仓库，默认约300～600中文字、明确接口调用表与最多五步流程；公式、原理及长示例放 `references/`。未绑定接口明确标注，不虚构工具。
- 模型选择目标和下一行动；会话、坐标转换、采样、数学计算与并发监控由 harness 承担。普通动作串行，未知副作用先核验，完成必须有证据。

[HARNESS_INTERFACES.md](docs/HARNESS_INTERFACES.md) 保留赛事阶段目标、进度卡、导航、reflex、compact/handoff等设计访谈与未冻结入口。当前 daily 运行时解决了部分基础链路，但不等于已经实现该文档中的全部未来赛事接口；`select_task`、`finish_task`、自动浮水/搭桥等不得作为现成工具调用。

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

消耗 5 只眼（64→59）。寻路曾入河超时、沉水并降到 18 生命值，随后通过正常上浮/游泳恢复到 20。最终记录为地面空闲；原会话当时保持在线，但跨机器不得假设它仍存在。全过程未用 RCON、管理员定位、存档或挖掘。

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

`setup` 下载固定官方1.5.2 JAR并检查 SHA-256，后续计算可离线；`.cache/ninjabrain/` 不提交。缺少引擎时测试会明确跳过，不能报告为引擎全部通过。合成示例指向 `[128,0]`，与实际世界无关。

`capture` 会登录、消耗末影之眼并在结束时登出，可能替换同名旧会话。需要实机采样时按 [STRONGHOLD.md](docs/STRONGHOLD.md) 使用自己的部署配置；持续 harness 进程应使用受协调的注入 client 入口，不能另起同名 CLI 采样。禁止把独立采样器误当作已注册的日常模型动作。

| 路径 | 用途 |
| --- | --- |
| `README.md`、`AGENTS.md` | 当前启动、验证边界及贡献/skill约定 |
| `docs/DAILY_SURVIVAL.md`、`docs/SURVIVAL.md`、`docs/TESTING.md` | 日常与初期生存契约、实际验证记录 |
| `docs/HARNESS_INTERFACES.md` | 未来赛事接口、已确认设计方向与未实现提案 |
| `integrations/ninjabrain/`、`skills/stronghold.py` | 独立官方计算桥接、采样器和CLI |
| `skills/stronghold_localization/SKILL.md`、其 `references/research.md` | 精简策略与误差研究，尚非运行时加载器 |
| `tests/test_stronghold.py`、`data/examples/stronghold_throws.json` | 要塞计算回归及合成输入 |
| `third_party/ninjabrain/` | 上游来源及GPL v3许可证 |
| `probe_api.py` | 早期探测脚本；不是当前主入口，运行前先核对其动作与局限 |

跨机器不复用历史PID、token、同名机器人会话或旧世界的投眼记录。游戏/API白名单、凭据、地址和存档由部署者配置；回环地址只表示当前执行机器。Git提交、远端同步和接收端完整性单独核验，本文不声称代码已推送或生产进程已加载合并结果。

# Repository Guidelines

## 项目结构与模块组织

BotSleep 基于 mc-agent-api 控制 Minecraft 机器人，采用大模型决策与本地技能分层设计。

- **用户约束（2026-11-14）**：不要修改或提交相邻 `mc-agent-api` 仓库。所有适配和修复在 BotSleep 内完成；上游行为不足时记录限制，不依赖本地 API 补丁。

- `main.py`：配置驱动的睡眠/生存入口；`survival.objective` 区分 starter、daily 与 competition。日常任务图在 `agent/daily_survival.py`，技能、观察、策略与 SQLite 记忆见 `docs/DAILY_SURVIVAL.md`。开启 `llm.enabled` 后使用原生工具调用与结果反馈。
- `core/`：HTTP 客户端、RCON、状态感知和记忆管理。
- `skills/`：移动、落地水和睡眠技能；遵循 `skills/__init__.py` 中的 `BaseSkill`、`SkillResult` 契约。
- `debug/`：聊天指令、RCON 靶场、脱敏结构化日志和本机只读监控；`data/game_logic/` 保存开发雷达/独立技能复用的状态、地标与事件记忆，不用于当前运行检查点，部分文件仍受版本控制。
- `docs/`：架构、技能契约和调试规范。修改相关行为前先阅读对应文档。
- `integrations/ninjabrain/`：无界面要塞计算与坐标采样；`tests/` 为 unittest 回归测试，`data/examples/` 存合成输入。
- `skills/portal_speedrun.py`、`skills/crafting.py`、`skills/mining.py` 和 `core/radar.py` 是已合并的独立/调试能力；独立 schema 不等于已绑定模型工具。模型目录仍为 starter 9、daily 33、competition 38；详见 `docs/SKILL_SPEC.md`。门户主动采样不等于只读 EventMonitor。
- 根目录 `bot_sleep.py`、`client.py` 是兼容入口，分别复用 `main.py` 和 `core/client.py`，不要再复制实现。
- `core/survival_harness.py` 串行执行生存任务，`core/survival_state.py` 按 API/账号/world_id 保存检查点；有 pending 意图时不得自动重放。`skills/survival.py` 的动作需要真实状态变化验证。
- `core/local_execution.py`、`core/local_tasks.py`、`core/local_recovery.py` 为启用模型的生存链提供本地执行优先：先本地安全，再任务/树冠脱困，明确失败或无进展后本地替代方案耗尽、无支持计划或预算耗尽后交模型；失败位置写内部 `failed_targets`，先换其他已观察且安全的资源目标，不盲重试原位置。`survival.local_execution` 默认 enabled=true、max_steps=64、max_seconds=180（秒），上限128步/600秒；每步新观察，沿用原 pending、安全复核与串行真实技能，不保存可重放队列。接管只传最近最多6条结果摘要；动作未知保留 pending 禁重放，模型门禁未知或请求在途时不本地生产，不作 provider 出错 fallback。比赛行动计划仍7个，工具仍9/33/38。
- `core/competition_rules.py` 与 `core/competition_tasks.py` 实现 competition 规则快照、advancement 证据计分、任务状态与终态恢复；只读面板见 `docs/MONITORING.md`。计分以服务端状态为准，条件项须显式 override。`model_view()` 给当前状态解锁的简化候选，选中后仅展开活动任务详情；完整125项目录仅用于本地核验和监控，不再默认注入模型。当前 45 个明确计分项目中仅 7 个有明确行动计划；其余目录虽可评估计分但标为 unsupported。不得把离线覆盖说成完整比赛执行验收。
- daily/competition 的无床/无庇护路线夜间开局可进入 `night_preparation`：满血、food>=12、无雷暴与至少12格威胁缓冲、完整观察和稳定落地，才开放3格内的单步准备。床/已验证返家、紧急安全优先；普通夜间入口不开放批量/探索/狩猎/清叶，显式force_attempt仅可按下述权限覆盖时间策略。策略、执行前校验和在途伤害监控必须一致，详见 `docs/DAILY_SURVIVAL.md`；不得通过移除底层技能校验来放行。
- daily/competition新增 `report_blocked` / `force_attempt`。force只覆盖有理由的时间/偏好策略，不覆盖明确危险、目标证据、库存/支撑、pending/模型锁/并发；nested动作需真实schema复核，原生call_id和未知结果持久化保持不变。有tools请求使用required，实际工具按当前约束/阶段裁剪，禁止自动回退auto；连续无进展wait转报告，赛事反复受阻可退出任务。
- `SurvivalHarness.next_poll_seconds` 供 `main.py` 调度：busy/无退避的本地ready最多等待50ms，本地成功中间步骤不加普通poll等待；失败退避、paused/finished不加速生产。聊天 `next_debug_poll` 按原配置间隔单独节流，不因快tick增加聊天API请求；不得把快tick宣称为20Hz实时反射或绕过安全/并发门禁。
- 模型只能选择已注册技能；返回后须重新观察并通过本地安全门禁。所有模型请求必须经过 `core/model_gate.py`，禁止并发请求；传输超时/连接中断留下持久化未知状态，不得自动重试或擅自清锁。明确 HTTP 失败可有界退避，不回退为规则采集。`llm.model` 是请求路由，实际模型仅采用 `response.model`，缺失时显示“未确认”。

安静树冠恢复新增 `core/canopy_geometry.py` 的 `plan_canopy_step` 与 `skills/canopy.py` 的 `CanopyRecoverySkill`。要求居中站在脚下干叶上，四正方向最多3格内找降低1～2格的落脚点；可先水平1格朝已核验下降口推进，不盲目 wander。最多清一格侧下方叶片，绝不挖自身支撑；daily collector 仅在 `canopy_blocked` 且无显式危险时采样，最多96次方块读/3秒规划预算。内部 Python 动作 `descend_canopy(pos, clear_pos, expected_name)` 只由 harness 本地分派，不注册模型工具，目录仍9/33/38。沿用串行 pending，每次写前复核12格实体门禁、支撑/身体净空，最多一次 `dig(collect=False)` 加一次 `move_to(range=0)`，精确落地确认；未知 dig 禁止 move 和重放。仅无近敌安静树冠使用新路径并忽略历史威胁方向，旧近敌 retreat 保留；实心地面清恢复，叶片则重观测。四格直落仍拒绝，不承诺任意树冠成功。侧下方权限不扩大 `clear_foliage` 的身体高度限制，也不扩大 generic clear/force 或临时别名范围。

树冠本轮现场仍 `posY=92`、本地 `no_verified_local_recovery`、0 steps，身体高度叶片已清、附近地面脚部 Y 为88；旧完整 retreat 的历史 threat 方向/实心终点约束会漏掉部分安全下降。离线覆盖含模拟 `92→91→90→88` 到 ground、零模型请求、competition selection 先恢复、未知 pending、内部动作非模型工具、规划预算与失败目标排除。最终离线结果见 `docs/TESTING.md` 首节，尚未实服验收。侧叶用例不是实服验收。本轮未重启或操控在线机器人，现场仅读日志和监控；当前进程需重启加载修改。

树冠恢复请求可临时提供绑定当前单个叶片目标的 `force_clear_foliage`（可选reason，`{}`有效）。它不在永久registry，坐标/名称由完整观察绑定且执行前复核，不从reason猜值；冷却/已失败目标跳过，不能借别名解除安全门禁。HTTP工具请求禁并行call，原alias/call_id仍需配对canonical force结果。恢复动作不应被无新危险的旧搜索冷却抢占。

## 构建、测试与开发命令

在仓库根目录使用 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe main.py --check-config
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe bot_sleep.py
```

前两步创建环境并安装当前代码依赖；两个启动命令复用同一配置驱动入口，默认自动睡眠。运行前需启动 Minecraft 服务及 mc-agent-api，默认接口为 `http://127.0.0.1:18900/api/v1`。模型规划需同时设置 `agent.mode: survival` 与 `llm.enabled: true`；`debug.monitor.enabled: true` 启用默认 `127.0.0.1:18930` 的只读监控。

`core/config.py` 读取 YAML，支持环境变量和用户名命令行覆盖。正常运行只依赖 requests 和 PyYAML；RCON 为可选开发依赖。配置、客户端、技能与调度器的接口应同步维护。

## 编码风格与命名约定

沿用 Python 四空格缩进；模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。公共接口添加类型注解与简短文档字符串。HTTP 交互集中在客户端，组合动作放入技能层，模型决策放入 `agent/`。技能失败通过 `SkillResult` 返回明确错误码，并处理超时和动作恢复。当前未配置格式化或 lint 工具，避免无关格式改动。

## 测试与验证

离线行为测试使用标准库 unittest，不连接真实游戏。提交前运行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

默认不启用实服测试。`tests/test_speedrun_portal.py` 的 3 项测试仅在 `BOTSLEEP_RUN_LIVE_TESTS=1` 时执行；`tests/test_crafting.py` 和 `tests/test_random_lava_matrix.py` 是直接运行的实服脚本，不是离线 unittest。它们会给物、传送或改地形，需专用世界、显式环境凭据与固定 SleepBot 账号。不得把跳过记为实服通过。隔离门禁回归见 `tests/test_live_test_isolation.py`。

涉及游戏行为时，在专用测试世界验证登录、状态读取和目标技能，并记录复现步骤、预期结果及日志。新增自动化测试放在 `tests/test_<module>.py`，模拟 HTTP、模型与 RCON 响应。搭高、落地水和高频反射仍未完成；自动睡眠、确定性规划与模型生存循环已接入主入口。必须区分协议/离线测试通过、真实技能成功和完整生存目标完成；模型返回工具调用不等于采集或制作成功。`#stop` 请求取消并暂停自动尝试，不保证后端所有动作瞬时终止。

要塞计算引擎另需 JDK 11+；执行 `python -m integrations.ninjabrain setup` 下载并校验固定版本 JAR 后再测试。缺少 JAR 时引擎用例会显式跳过，报告必须区分通过和跳过，不能算作完整引擎验证。

## 提交与拉取请求

现有提交包含 `init` 和 `feat: ...`，尚无完整强制规范；建议使用 `feat:`、`fix:`、`docs:` 等前缀，概括单一变更。拉取请求应说明目的、影响模块、验证命令和结果，关联相关 issue；涉及游戏行为时附日志或截图，并同步更新技能契约或架构文档。

## 配置与数据安全

不要提交真实 API 密钥、机器人密码或 RCON 凭据。`config.yaml`、`.env`、`.venv/` 和 `logs/` 已被忽略。`data/game_logic/` 中的数据受版本控制，提交前检查是否混入本地运行状态或私人服务器信息。

计算与采样说明见 `docs/STRONGHOLD.md`。纯计算不连接游戏；`capture` 会消耗末影之眼并登录后登出。生成的 `.cache/ninjabrain/` 不提交，上游许可证见 `third_party/ninjabrain/`。

`calculate_stronghold` 仅保留独立 schema 常量、Python Skill 和 CLI，不在 starter/daily 模型工具目录中。接口方向与赛事运行时草案见 [HARNESS_INTERFACES.md](docs/HARNESS_INTERFACES.md)；已实现的日常运行时不代表这些未来接口已全部实现。

## Harness AI Skill 编写约定

后续 skill 默认面向小模型：

- 正文简短，以明确目标、调用表和不超过五步的流程为主；默认争取约 300～600 中文字，必要接口示例可适量超出。
- 接口写明真实名称或 HTTP 方法/路径、参数示例、单位及关键返回字段。尚未绑定的入口必须注明，不虚构可调用工具。
- 原理、公式、来源和长示例放入 `references/`，不默认注入模型上下文。只给当前任务需要的状态与结果摘要。
- 会话管理、坐标转换、采样、数学计算及并发监控由 harness 负责；skill 只让模型选择行动，并给出明确的完成、失败和中止条件。

独立只读日志监控可用 `python -B -m debug.monitor_tail --log-dir logs --port 18931`，不连接游戏/模型或读配置；只跟踪指定flight日志，页面明确标注来源，不补造心跳。关闭它不控制机器人。原生运行监控仍需 `debug.monitor.enabled: true` 且在启动时加载，默认18930。

## 文档状态同步

- `docs/ARCHITECTURE.md` 描述当前 starter/daily/competition 运行链路；`docs/HARNESS_INTERFACES.md` 分开记录现有比赛得分任务子集和未来提案。不得因旧 `agent/loop.py` 有 TODO 就宣称生存主循环未接通。
- 当前只读面板聚合 heartbeat、Agent 阶段、模型槽、pending、competition 任务快照；API 端点延迟、检查点写入健康度、无进展/重复输出告警与可靠性汇总仍待补。历史裁剪按请求字节预算执行，不等于 Pi/OMP 式语义 compact；阶段目标清单、handoff、Markdown skill 加载、水面优先导航和自动搭路仍待实现。
- 本地执行摘要来自 `snapshot.local_execution`，审计事件为 `LOCAL_EXECUTION_STEP` / `LOCAL_EXECUTION_RESULT` / `MODEL_TAKEOVER`；`EventMonitor.local_execution` 投影已发布摘要；HTML 本地执行卡片显示阶段/步数/原因/近期动作，当前决策来源只采用 `DECISION.data.source`，缺失显示未确认，不据 planner 或接管事件猜测来源。完整 starter 链路离线验证通过，零模型请求；本轮最终结果以 `docs/TESTING.md` 首节为准。未做实服验收，不代表完整比赛执行验收。
- 测试结果注明源码基线、环境与是否本轮复核；历史全绿不能覆盖另一环境中的失败。最新结果与已知差异统一放 `docs/TESTING.md`。

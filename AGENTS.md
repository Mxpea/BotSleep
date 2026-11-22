# Repository Guidelines

## 项目结构与模块组织

BotSleep 基于 mc-agent-api 控制 Minecraft 机器人，采用大模型决策与本地技能分层设计。

- **用户约束（2026-11-16）**：允许拉取相邻 `mc-agent-api` 的官方更新；不要在该仓库修改或提交本地源码补丁。所有适配和修复在 BotSleep 内完成；上游行为不足时记录限制，不依赖本地 API 补丁。

- `main.py`：配置驱动的睡眠/生存入口；`survival.objective` 区分 starter、daily 与 competition。日常任务图在 `agent/daily_survival.py`，技能、观察、策略与 SQLite 记忆见 `docs/DAILY_SURVIVAL.md`。开启 `llm.enabled` 后使用原生工具调用与结果反馈。
- `core/`：HTTP 客户端、RCON、状态感知和记忆管理。
- `skills/`：移动、落地水和睡眠技能；遵循 `skills/__init__.py` 中的 `BaseSkill`、`SkillResult` 契约。
- `debug/`：聊天指令、RCON 靶场、脱敏结构化日志和本机只读监控；`data/game_logic/` 保存开发雷达/独立技能复用的状态、地标与事件记忆，不用于当前运行检查点，部分文件仍受版本控制。
- `docs/`：架构、技能契约和调试规范。修改相关行为前先阅读对应文档。
- `integrations/ninjabrain/`：无界面要塞计算与坐标采样；`tests/` 为 unittest 回归测试，`data/examples/` 存合成输入。
- `skills/portal_speedrun.py`、`skills/crafting.py`、`skills/mining.py` 和 `core/radar.py` 是已合并的独立/调试能力；独立 schema 不等于已绑定模型工具。模型目录为 starter 9、daily 35、competition 40；详见 `docs/SKILL_SPEC.md`。门户主动采样不等于只读 EventMonitor。
- 根目录 `bot_sleep.py`、`client.py` 是兼容入口，分别复用 `main.py` 和 `core/client.py`，不要再复制实现。
- `core/survival_harness.py` 串行执行生存任务，`core/survival_state.py` 按 API/账号/world_id 保存检查点；有 pending 意图时不得自动重放。`skills/survival.py` 的动作需要真实状态变化验证。
- `core/autonomous_tasks.py` 是无模型/客户端 I/O 的有界目标程序；`core/autonomy_runtime.py` 把 `run_survival_task` 接入现有 harness，保留一次原生目标反馈，每个子步骤仍独立 fresh 检查和 pending。默认 15 类目标、128 步/600 秒、4 次连续本地失败/8 次无进展/8 次探索；紧急安全挂起目标，unknown 不重放。只覆盖明确常用配方子集，不能宣称全部配方或全部任务实服通过；见 `docs/AUTONOMOUS_TASKS.md`。
- `core/domain.py`、`core/skill_registry.py` 统一共享值和技能目录，必须保留主线自主任务、战斗与桶类入口；`core/retreat_controller.py` 管分段退避，`core/local_recovery_runtime.py` 管树冠/觅食恢复，不再复制旧控制器。住所采用 `StructureSpec` 四朝向蓝图，默认 north 兼容旧站点；门、床、箱子和返家证据必须使用同一朝向。
- `skills/action_workflow.py`、独立门户/矿井/洞穴/容器、`core/world_3d.py` 与 `skills/world_navigator.py` 是结构工作流及 3D 探索能力，未默认注册到日常模型或替换地表采集。未知写入必须跨工作流传播，禁止通过新建 guard、换候选、返程或姿态清理追加动作。3D 记忆不替代新鲜观察；持久化调用须显式提供正确服务器、账号、世界和维度作用域。
- `SurvivalJournal.instance_lock()` 防止同作用域重复启动；`scripts/botctl.py` 只读检查实例、游戏 pending 和模型门禁。会话保持仅由 `--keep-connected` / `mc_api.keep_session_on_exit` 显式开启，competition 默认退出仍登出。
- autonomy 正常规划采用实际动态工具目录：目标工具与必要安全/赛事管理工具可见，`needs_ai` 后开放原子修复；不只依赖 prompt 的 prefer。9/35/40 是完整注册数，不是每次模型请求的暴露数；禁用 autonomy 保留原单步目录。
- `core/competition_rules.py` 与 `core/competition_tasks.py` 管理规则、服务端 advancement 计分、任务状态及恢复。45 个无条件计分项中 13 个已有工具路线；其余 32 个及 54 个条件项在 `core/competition_blueprints.py` 有步骤和能力缺口，但不可选择。条件项计分仍须显式 override。见 `docs/HARNESS_INTERFACES.md`；不得把蓝图或离线回归说成实服执行验收。
- `skills/combat.py`、`skills/hunting.py`、`skills/buckets.py` 分别实现短步战斗、静止狩猎和受限液体操作。攻击已发、伤害已证实、击杀与实际掉落增量分开；未知副作用保持 pending，不补发 stop/攻击/收桶。`core/survival_playbook.py` 的版本化阶段建议经 `survival.intent` 选择，未绑定的下界/末地技能保持 blocked；参见 `docs/COMBAT_AND_HUNTING.md` 与 `docs/SURVIVAL_PLAYBOOK.md`。
- 模型只能选择已注册技能；返回后须重新观察并通过本地安全门禁。所有模型请求必须经过 `core/model_gate.py`，禁止并发请求；传输超时/连接中断留下持久化未知状态，不得自动重试或擅自清锁。明确 HTTP 失败可有界退避，不回退为规则采集。`llm.model` 是请求路由，实际模型仅采用 `response.model`，缺失时显示“未确认”。

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

默认不启用实服测试。`tests/test_speedrun_portal.py` 的 3 项与 `tests/test_cave_matrix.py` 的 2 项测试仅在 `BOTSLEEP_RUN_LIVE_TESTS=1` 时执行；`tests/test_crafting.py` 和 `tests/test_random_lava_matrix.py` 是直接运行的实服脚本，不是离线 unittest。它们会给物、传送或改地形，需专用世界、显式环境凭据与专用账号。不得把跳过记为实服通过。隔离门禁回归见 `tests/test_live_test_isolation.py`。

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

## 文档状态同步

- `docs/ARCHITECTURE.md` 描述当前 starter/daily/competition 运行链路；`docs/HARNESS_INTERFACES.md` 分开记录现有比赛得分任务子集和未来提案。不得因旧 `agent/loop.py` 有 TODO 就宣称生存主循环未接通。
- 当前只读面板聚合 heartbeat、Agent 阶段、模型槽、pending、competition 任务快照、生存阶段建议及最近战斗证据；API 端点延迟、检查点写入健康度、无进展/重复输出告警与可靠性汇总仍待补。历史裁剪按请求字节预算执行，不等于 Pi/OMP 式语义 compact；独立结构工作流支持受限搭柱/搭桥，生存模型内的水面优先导航、自动 handoff 与 Markdown skill 加载仍未接通。
- 测试结果注明源码基线、环境与是否本轮复核；历史全绿不能覆盖另一环境中的失败。最新结果与已知差异统一放 `docs/TESTING.md`。

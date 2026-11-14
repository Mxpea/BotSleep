# BotSleep 评估交接文档

> **2026-11-14 更新**：用户明确要求不修改 `mc-agent-api`。此前本地 `src/session.js` 修改及新增 `src/navigation.js`、`test/navigation.test.js` 已撤回，未提交或推送；原有未跟踪 `package-lock.json` 保留。后续只在 BotSleep 侧适配原版 API。下文 13 项 API 测试仅为已撤回实验的历史结果，不是当前依赖或当前可运行检查。

> **最新暂停点（2026-11-13 22:52，Asia/Shanghai）**：用户准备断电，要求完成当前修复后停止。机器人、监控及本机 API 已停止；Minecraft 已执行 `save-all flush` 和 `stop`，日志确认所有维度保存，Java 进程已退出。未设置自动重启，等待用户再次授权恢复。
>
> 模型已接入生存主流程，`default` 的真实响应字段为 `qwen3.6-35b-a3b`，原生工具调用与失败反馈已发生。BotSleep 保留的修复：触达信息兼容、无位移结果与失败记忆、实际库存采集证据、有限安全站位/放置候选，以及历史完整观察导致 51,524 tokens 超过 43,776 上限的 HTTP 400。BotSleep **293 项离线测试通过**。上游空路径行为及视线字段补丁已撤回；额外 `can_dig/can_see/collection` 元数据均为可选，不要求原版 API 提供。**修复后的真实自主采集→制作闭环尚未验证**，不可引用此前确定性靶场结果代替。
>
> 恢复时先启动 `.test-runtime/leaves` 的游戏服务，再启动原版 `../mc-agent-api/src/index.js`（配置 `.test-runtime/api-config.json`），最后使用 `.test-runtime/llm.yaml` 启动 `main.py --mode survival`。监控端口 18930，API 18920，游戏 25585；私密配置已忽略，不要打印凭据。检查 checkpoint.pending 后再执行；普通世界仍禁止破坏性测试布置。诊断证据在 `.test-runtime/pathing-terrain-inspection.json`、模型探测 JSON、`navigation-offline-tests.log` 和原 flight 日志中。

> 历史评估记录：下文涉及其他工作区的路径与当时状态。当前 `D:\Code\Python\MC-Agent\BotSleep` 已继续实现配置加载、统一客户端和自动睡眠主流程；配置、客户端、睡眠与测试的最新使用说明见 README 和 `docs/TESTING.md`。下文的“未授权修复”“当前尚未实现”等描述不代表后续用户授权或最新代码状态。

> 当日后续：用户已授权实现正常生存模块与 harness，并要求普通地图。现已接入确定性初期工具链、六项验证技能、观察/持久化/失败恢复/预算，197 项离线测试通过，最终真实空背包验收 38 步完成石镐/石斧/石剑/熔炉。详见 `docs/SURVIVAL.md`、`docs/TESTING.md`。这不是完整长期生存或比赛全部目标。25585 的 `.test-runtime` 已切换 `botsleep-survival-world`，是用户正在玩的普通世界，**不得运行破坏性旧睡眠测试或拿来搭测试场**；旧平坦世界保留。独立 `.survival-test-runtime` 验收服务已关闭，报告与存档保留。在线 SleepTestBot 仍运行 sleep 模式；新普通世界的 survival 配置已另备于 `.test-runtime/survival.yaml`，未启动第二个同名机器人。

更新日期：2026-11-13（Asia/Shanghai）。已补充当日 19:53 的本地真实服务测试。本文记录架构评估、验证证据和下一步，接手者无需阅读之前的对话。

## 1. 背景与目标

BotSleep(`D:\.WORKSPACE\BotSleep`,Python)不是用户写的。用户想基于它,或参考它,做一套通用的 Minecraft Agent harness,参加"Minecraft Agent 挑战赛初赛"(规则草案 v2)。当前阶段只做评估与验证。用户已授权测试其本地 API 和 Minecraft 服务，并要求更新本文；**尚未授权重写或修复项目源码**。

比赛要点:

- 8 小时,每人一座完全相同的虚空岛,版本 1.21.11。
- 总分 100 = 游戏表现分 60 + 工程评审分 40。
- 游戏表现分只看服务器记录的 advancement。每项分值只由难度决定:1/2/3/4/5 级分别是 5/15/30/60/100 分。按本届最高原始分归一化到 60 分。
- 工程评审分由不少于 3 名评委对 harness 和 Agent 工程打分,看设计完善性、新颖性、代码清晰度、稳定性与可维护性。去掉最高最低各一个后取平均。
- 并列时先比难度 5 的完成数量,再依次比难度 4、3、2、1。
- 比赛期间禁止服务器命令、RCON、管理员权限和读取存档。RCON 只能用于开发期。
- 规则全文在 `uploads\RULES.md`(用户上传)。仓库里 `G:\ww\mc-agent-api\docs\RULES.md` 是措辞不同的另一份副本。只比对了前约 30 行差异,没发现分值变化,没有完整比对。

## 2. API 事实(来源:`G:\ww\mc-agent-api`,v1.3.3)

契约文档是 `docs/API.md`。技术栈为 Node ≥20、mineflayer 4.39.0、mineflayer-pathfinder。API 不能改,只能在 harness 一侧适配。

- 基础路径 `http://127.0.0.1:18900/api/v1`。先 `POST /auth/login` 换 Bearer token,登录后 bot 自动连接,轮询 `GET /status` 直到 `ready: true`。
- 响应格式为 `{ok, ms, result | error}`。部分业务错误也返回 HTTP 200,所以必须检查 `ok`。
- 动作接口是阻塞的高层动作,带自动寻路,同一时刻只能执行一个,否则返回 `busy`。动作默认 `stop_on` 为 `["hurt","death"]`,`attack` 和 `shoot` 默认只在 `death` 时中断。
- 观察接口(`status`、`inventory`、`find_blocks`、`get_entities`、`events`、`advancements`、`get_block`、`stop`、`connect`、`disconnect`、`posture`、`look`)不占动作锁。
- `/events` 是 500 条环形缓冲,按自增 `seq` 取,用上次的 `last_seq` 作下次 `since`。
- `/status` 没有速度字段。`/control` 最短按键时长 50ms,没有逐 tick 控制。
- 死亡循环保护:60 秒内死 3 次,bot 离线 5 分钟。重连间隔至少 30 秒。同一角色重复登录会让旧 token 失效。
- 当前本地配置(`config.json`):监听 127.0.0.1:18900,Minecraft 服务器 127.0.0.1:25565,离线认证,只允许用户名 `Agent_gugugaga`,口令见 `config.json`。

## 3. BotSleep 当前实现

设计与模块职责见 `README.md`、`docs/ARCHITECTURE.md`、`docs/SKILL_SPEC.md` 和 `AGENTS.md`。注意区分设计目标与可运行功能。

- `core/client.py` 和根目录 `client.py` 使用的 `/blocks`、`/entities`、`/look_at` 均不兼容当前 API。正确调用是 `POST /find_blocks`、`POST /get_entities`、`POST /look`。方块和实体封装通过 `.get("result", {})` 将服务错误吞成空列表。
- `core/client.py` 尚缺 `/stop`、`/inventory`、`/dig`、`/craft`、`/equip`、`/window/*`、`/advancements` 等能力。
- `main.py` 接受 `--username`，默认 `SleepBot`；base_url 和密码仍硬编码，`--config` 未加载 YAML。默认账号不在当前服务白名单内；只改用户名也没有解决口令配置问题。
- 主入口未启动 `AgentLoop` 或 `ReflexDaemon`；其思考、技能分发和应急检测仍是 TODO。游戏内 `#stop` 也只有提示，没有发送停止请求。
- 三个宏技能仍为占位实现，能在未执行相应游戏动作时返回成功，见下节隔离检查。
- 根目录 `bot_sleep.py` 有睡眠循环结构，但依赖同样错误的床位查询接口。此前“唯一能跑通的流程”的判断已撤回，当前未完成端到端睡眠验收。
- `data/game_logic/world_context.json` 曾记录方块数和实体数为 0、biome 为空。这只能作为历史现象，不能单凭缓存内容确认原因。

## 4. 真实服务验证结果

证据：`logs/live_test_result.json`，记录时间 `2026-11-13T19:53:24+08:00`。接口摘要另存于 `logs/probe_result.json`。两份报告均不含口令和 token，且 `logs/` 被 Git 忽略。

### 已在本地服务验证

| 项目 | 实测结果 |
|---|---|
| 未鉴权 `GET /status` | HTTP 401，`unauthorized` |
| Python 语法检查 | 21 个文件通过 AST 解析；不代表功能通过 |
| `.venv\Scripts\python.exe main.py --debug` | `invalid_credentials / username not allowed`，随后退出；进程退出码却为 0 |
| 使用服务配置构造现有客户端 | `Agent_gugugaga` 登录成功，约 0.55 秒进入 `ready`，生命值和饥饿值均为 20 |
| `GET /blocks`、`GET /entities`、`POST /look_at` | 全部 HTTP 404，`unknown_api` |
| `POST /find_blocks`、`POST /get_entities`、`POST /look` | 全部 HTTP 200，`ok: true` |
| 对比方块查询 | 正确接口在半径 8 内返回 5 个方块；两套项目客户端均返回空列表 |
| 项目状态和事件封装 | `TelemetryManager.poll()` 得到 `ready: true`，`get_events()` 返回 3 条事件 |
| `GET /events` | 返回 `events/has_more/last_seq/since/total`；本次类型为 `chat/server_confirmed/spawn`，`last_seq=3` |
| `POST /advancements` | `ready: true`；返回 `advancements/packets_received/ready/summary/updated_at`，本次非配方总数和完成数均为 0，隐藏配方 2 项 |
| 空闲 `POST /stop` | `ok: true`，`stopped: true` |
| 清理会话 | 登出成功；最终位置与初始位置一致，生命值仍为 20 |

`/status` 实际返回位置、生命值、昼夜、天气、地面状态、`busy` 等字段，完整列表见报告；没有速度字段。advancement 返回为空仅描述本次查询，不能据此认定服务器没有可完成的 advancement。

### 技能隔离检查（非实机动作验收）

使用仅提供状态读取、禁止动作调用的替身客户端：

- `PillarUpSkill.execute()`：零客户端调用，直接返回搭高成功。
- `MLGWaterSkill.execute()`：零客户端调用，直接返回落地水成功。
- `AutoSleepSkill.execute()`：模拟夜间，仅读取一次状态便返回入睡成功。

这证明存在空报成功，不能说明搭高、落地水或睡眠实际可用。

### 仍未验证

- 并发动作的 `busy`、动作期间观察接口是否畅通、`/stop` 是否真正打断动作。原计划用最多 3 秒的原地潜行测试，但状态未满足安全站立检查，因此整体跳过，**没有发送 `/control`**。报告未保存具体哪一项前置条件不满足。
- 事件缓冲溢出、分页和游标连续性；目前只确认一次小样本响应结构。
- 死亡保护、掉线重连、长期稳定性，以及真实模型调用和完整自主游玩。
- 实际找床、寻路、入睡及清晨唤醒。

## 5. 探测脚本与测试环境

- 早期 Linux 环境不能访问 Windows localhost；当前 Windows 会话已能访问，旧网络阻塞不再适用。
- `probe_api.py` 已对真实服务运行。首次完整调用后，写根目录 `probe_result.json` 被沙箱拒绝；`finally` 中已先完成登出。本轮随后直接调用其探测函数，并将结果保存到 `logs/`。
- **脚本仍有误判**：`status_has_velocity` 用 `"vel" in 字段名` 检测，会匹配 `xp_level`。看到 `true` 不能视为速度存在；本轮报告使用明确速度字段名核对，结果为 `velocity_fields: []`。脚本未修改。
- 该脚本默认会登录、转向、调用空闲 `/stop` 并登出，并非严格只读；`--lock-test` 会原地跳跃，应先确认环境。重复登录可能使同名旧 token 失效。
- 初始无 `.venv`，系统 Python 3.11 有 `requests`，缺 `mcrcon`，导致主入口在导入 `core.rcon` 时退出。已创建项目 `.venv` 并安装 `requests`、`mcrcon` 及依赖，当前 `.venv\Scripts\python.exe main.py --help` 可运行。
- 虚拟环境初始化和自带 pip 曾异常；默认 PyPI 又发生 TLS 错误，最终通过系统 pip 的 `--target .venv\Lib\site-packages` 和清华 HTTPS 镜像安装成功。尚未证明虚拟环境自身 pip 已恢复正常。
- 综合测试通过临时标准输入脚本运行，未保存该脚本；JSON 报告可检查，但目前没有一条命令复现全部检查。

本轮没有修改 Python 源码或本地服务配置，没有执行 RCON、挖掘、放置、战斗或模型请求。执行过登录、观察、转向、空闲停止及登出；未留下测试机器人在线。

## 6. 评估结论与下一步

- 保留“LLM 决策 + 本地技能”的方向，但当前代码不足以作为可用的自主比赛 Agent。
- API 没有速度字段，且控制与动作锁有限制，现有证据不足以支持设计文档承诺的 20Hz 实时应急能力；不要将其写成已实现或已验收。
- 技能优先级应结合 advancement 分值、前置成本和稳定性确定。此前评估认为睡眠直接得分低，但落地水、搭高等生存动作的间接收益仍需结合赛图判断；规则分值以最终全文为准。
- 工程评审占 40%，错误可见性、日志、可复现测试、死亡与重连恢复都值得优先建设。比赛执行路径不能依赖 RCON。

建议后续顺序：

1. 向用户报告并确定修复范围；目前仅授权实测和更新交接，不代表批准重写。
2. 如获准修复，优先处理配置加载、API 路径与响应错误检查，并让启动失败返回非零退出码；统一两套客户端的使用边界。
3. 为错误响应和技能完成判定补可复现测试，避免 `404 → []`、未执行动作却返回成功。
4. 在安全位置补测动作互斥和停止，再根据用户选择验收睡眠流程或推进比赛基础能力；不要把聊天 `#stop` 当作可靠的紧急停止。
5. 修正探测脚本速度误判，整理可重复运行的实测入口，并按需完整比对两份规则。

## 7. 工作区与注意事项

- 已新增中文贡献指南 `AGENTS.md`。`HANDOFF.md`、`probe_api.py` 和 `AGENTS.md` 当前均为未跟踪文件；未创建提交。
- `.venv/`、`logs/`、`__pycache__/` 为本地环境或测试产物，受 `.gitignore` 忽略；测试启动主入口产生的飞行日志也在 `logs/` 下。
- 凭据只从 `G:\ww\mc-agent-api\config.json` 读取，不要将真实密码或 token 写入文档、报告或代码。
- 外部 API 仓库的 `test/live_test.py` 据前次交接为需 RCON 的 31 项验收套件，本轮未运行；只能在专用开发测试服使用。
- 用户要求中文沟通。后续授权以用户最新指令为准，不应把交接中的建议当作已授权执行的任务。

## 8. 文件索引

| 路径 | 说明 |
|---|---|
| `D:\.WORKSPACE\BotSleep\HANDOFF.md` | 本交接文档 |
| `D:\.WORKSPACE\BotSleep\AGENTS.md` | 中文贡献指南 |
| `D:\.WORKSPACE\BotSleep\probe_api.py` | 标准库探测脚本，含已知速度字段误判 |
| `D:\.WORKSPACE\BotSleep\logs\live_test_result.json` | 本轮综合实测证据 |
| `D:\.WORKSPACE\BotSleep\logs\probe_result.json` | 本轮接口探测摘要 |
| `G:\ww\mc-agent-api\docs\API.md` | API 契约 |
| `G:\ww\mc-agent-api\config.json` | 服务配置及凭据（不要输出全文） |
| `G:\ww\mc-agent-api\src\http.js`、`src\session.js` | 路由与动作实现 |

## 9. 建议技能

- `diagnose`：用户要求修复已复现故障时，用于建立失败信号并验证修复；本轮未进入源码修复。
- `tdd`：用户要求测试先行时，用于客户端错误处理和技能成功判定的回归测试。
- `handoff`：继续维护交接，保留证据来源、未验证范围及授权边界。

# BotSleep / Minecraft Agent Harness 交接

更新日期：2026-11-13（Asia/Shanghai）。面向不同机器上的协作者；文中代码和文档路径均相对本仓库根目录。外部依赖使用项目名指代，部署地址、账号和凭据应由接手者自行配置。

## 1. 目标、用户偏好与当前结论

目标是基于 BotSleep 建设通用 Minecraft Agent harness，并评估用于 Minecraft Agent 挑战赛的可行性。用户已授权 API 实测、Ninjabrain Bot 计算集成、真实投眼定位和模型接口测试。当前最新任务是整理交接，未要求立即继续游戏操作或实施完整 harness 重写。

**现状：模型工具调用和要塞采样/计算分别验证可用，尚未接成小模型自主游戏闭环。** 真实要塞测试由编码助手直接编排程序完成，不能算模型自主成功。

用户要求：

- 中文沟通；面向游戏 AI 的 skill 放在本仓库，不放个人 Codex 技能目录。
- 默认面向小模型，短正文、明确调用表、最多五步流程。公式、研究和长示例放 `references/`，不默认注入上下文。
- skill 明确真实接口、参数、单位和返回字段；尚未绑定的入口不得假装可调用。
- 采样、计算、会话与动作监控由程序负责，模型负责选择下一步。
- 协作交接不能依赖原机器的路径、进程、执行会话或仍在线的机器人。

比赛规则来自早期交接，尚未在本轮重新完整核验：8 小时、Java 1.21.11、相同虚空岛；游戏表现 60 分按 advancement 计，工程评审 40 分。难度 1～5 的条目原始分为 5/15/30/60/100，游戏表现按最高原始分归一化。比赛禁止服务器命令、RCON、管理员权限和读取存档，RCON 仅用于开发测试。最终以主办方规则为准；两份规则副本此前未完整比对，上传材料不保证随仓库分发。

## 2. 架构方向与实际实现

目标流程：

`状态/事件 → 精简上下文与任务记忆 → 当前任务 skill + 小模型 → 工具校验/分发 → 程序技能 → 游戏 API → 真实结果摘要`

动作执行期间，程序并发观察进展、血量和氧气，必要时停止/恢复。普通动作串行执行；模型超时可有限重试，已发出的游戏动作不能盲目重发，先检查是否已经执行。

| 模块 | 当前状态 |
|---|---|
| `agent/brain.py` | OpenAI 兼容 Chat Completions 封装；支持 messages、tools、自动工具选择 |
| `agent/tools.py` | 部分工具 schema 已有，包含 `calculate_stronghold`；不是可执行工具注册表 |
| `agent/loop.py` | 仅轮询状态，提示构建、模型调用、工具分发均为 TODO |
| `main.py` | 未启动 AgentLoop；只连接机器人及可选聊天调试；`--config` 尚未读取 YAML |
| `core/client.py` | 部分接口可用，部分旧路径仍错误，见第 3 节 |
| `core/telemetry.py` | 状态缓存已有；ReflexDaemon 检测是 TODO |
| `core/memory.py`、`debug/logger.py` | JSON/JSONL 状态、地标、事件记忆和日志基础设施存在，未统一接入自主闭环 |
| `skills/stronghold.py` | 纯要塞计算执行器，返回 SkillResult |
| `integrations/ninjabrain/` | 官方计算核心桥接、轨迹采样、CLI 已实现 |
| `skills/stronghold_localization/SKILL.md` | 面向游戏 AI 的精简策略；尚无自动加载器 |
| 原有睡眠、搭高、落地水技能 | 仍含占位成功返回，不可当作可靠动作 |

`docs/ARCHITECTURE.md` 是早期设计，关于 20Hz 反射、实时落地水等描述不是已实现能力。当前 API 没有速度字段；应先建设动作监控和有限恢复，再评估实时能力。

## 3. 游戏 API 契约与已知问题

外部依赖项目名为 `mc-agent-api`。早期源码审查基于其 v1.3.3；其他机器应核对实际部署的 `docs/API.md` 和实现版本。API 不能修改，适配在 harness 一侧完成。

- 基址由部署配置提供，路径前缀 `/api/v1`。`POST /auth/login` 换 Bearer token，然后轮询 `GET /status` 至 `ready: true`。重复登录同名角色可能使旧 token 失效。
- 检查响应 `ok`，不能只检查 HTTP 状态。部分业务错误使用 HTTP 200。
- 动作高层、阻塞、互斥，第二个动作可能返回 `busy`。观察接口以及 stop、posture、look 可在动作期间调用。
- `/events` 按 `seq` 游标读取；早期审查为 500 条环形缓冲，分页与溢出尚未实测。
- `/control` 最短 50ms、最长 3000ms。`move_to` 使用 `timeout`（毫秒），不是 `timeout_ms`；省略 y 时按水平目标寻路。
- `posture` 是全量姿态设置，未指定字段会复位；动作结束会重放持久姿态。水中恢复时不要过早解除 jump。
- 两套 yaw 不同：控制为 mineflayer 弧度 `atan2(-dx,-dz)`；投眼为原版度数 `degrees(atan2(-dx,+dz))`。
- 源码审查有死亡循环和重连限制，实测前核对部署配置；未验证完整死亡/重连恢复。

已确认及仍待修复：

1. 原 `GET /blocks`、`GET /entities`、`POST /look_at` 真实返回 404 `unknown_api`；正确路径是 `POST /find_blocks`、`POST /get_entities`、`POST /look`。
2. 集成时仅修正了 `core/client.py.get_entities`，新增 `get_inventory`，并为这些调用和 `use_item` 增加业务错误检查。`get_blocks`、`look_at` 以及根目录旧 `client.py` 仍需适配。
3. `core/client.py.move_to` 仍发送 `timeout_ms`，测试通过正确 HTTP 请求绕过，不能声称封装已修复。
4. `main.py` 默认用户名和密码硬编码，曾因部署白名单拒绝登录；异常退出码却为 0。配置模板修改不会自动生效。
5. `#stop` 仍只是聊天提示，不是真正停止。不要依赖它紧急中断。
6. `probe_api.py` 用子串 `vel` 检测速度，会误匹配 `xp_level`；该 bug 尚未修改。它默认会登录、转向、空闲 stop 和登出，并非严格只读。

## 4. Ninjabrain Bot 集成

详细使用与边界见 `docs/STRONGHOLD.md`；上游来源和 GPL v3 许可见 `third_party/ninjabrain/NOTICE.md`、`third_party/ninjabrain/LICENSE`。

- 固定官方 1.5.2 JAR，校验 SHA-256 后缓存于 `.cache/ninjabrain/`，通过 JSON 子进程调用无界面 Java 桥接。
- 直接使用官方 Posterior，保留生成环、生物群系偏移、角度误差和最近要塞约束；不启动 GUI、剪贴板或热键。
- 适用原版 Java 1.19+ 主世界，坐标观测绕过人工准星修正；不接入 divine、船眼或下界盲穿功能。
- `EyeObservationCollector(client).capture()` 消耗一只眼，按名字和 ID 跟踪一只新眼的多点轨迹，输出 `throw/quality/diagnostics`。
- `CalculateStrongholdSkill().execute(throws=[...], top_k=2)` 只计算，返回 `success/data/error_code`；候选在 `data.candidates`。
- CLI `capture` 会自行登录并在结束时登出；持续 harness 会话应使用注入 client 的 Python 入口，避免顶掉会话。

API 的 `use_item(ender_eye)` 会返回 `landed_at/eye_vector/bearing_deg`，但源码选择最近的新实体，可能误认；采样器不直接依赖该方位角。API 坐标舍入到 0.1 方块，方位角取整到 1°，因此应从坐标拟合方向。

## 5. 已完成的验证

### 基础与离线验证

- 基线 21 个 Python 文件语法解析通过。依赖安装后 `main.py --help` 可运行，但默认实机启动因账号不匹配失败。
- 真实登录 ready、状态、事件、advancement 查询成功；正确方块查询找到内容，旧客户端却把错误吞成空结果。
- 空闲 stop 成功。后续移动实测中 stop 能中断寻路；并发第二个动作是否返回 busy 尚未系统验收。
- 原有三技能隔离测试证明：搭高和落地水零动作调用便报成功；睡眠仅检查夜间便报成功。
- `tests/test_stronghold.py` 在安装官方 JAR 后，最近一次运行 **24 项通过**：方向/负坐标、轨迹拟合、实体歧义、业务错误、依赖校验、真实官方引擎、基线变化、矛盾观测及离线 CLI。
- 合成示例 `data/examples/stronghold_throws.json` 的首选为 `[128,0]`，与真实世界无关。缺 JAR 时引擎测试会显式跳过。

### 五眼真实定位

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

## 7. 小模型接入实测

用户提供的模型服务：`https://api.nju-aia.com/v1`。该地址是本次服务选择，不是 harness 应硬编码的通用默认。凭据从私有 `.env` 读取，不能写入交接、日志或代码。

- `GET /models` 认证成功，仅列出模型 ID `default`。配置继续使用 `default`。
- 成功 Chat Completions 响应中的模型标识为 `qwen3.6-35b-a3b`；这是服务返回标识，不是独立验证的底层部署事实。
- 模拟 `move_to` 正确生成 x/z/range/timeout 参数，耗时约 2.69 秒。
- 模拟 `get_status` 正确返回空参数工具调用，约 3.38 秒；回传模拟状态后，约 1.19 秒准确回答血量和坐标，没有再次调用。
- 完整基础 tool-calling 往返已通过，但不是游戏自主任务验收。未发送真实游戏动作。
- 服务曾返回 403/1010、502 和连接/读取超时，后续请求才成功；需实现有限重试、超时与可观测错误。不能将某次成功当作稳定性通过。

每位协作者自行配置：

```dotenv
LLM_BASE_URL=https://api.nju-aia.com/v1
LLM_MODEL=default
LLM_API_KEY=<自行获取并保密>
```

`.env`、`config.yaml` 都被忽略，不随仓库分发。**目前没有自动加载 `.env`/YAML 并启动模型循环的完整入口**，上述配置是约定，不代表写入后主程序自动生效。接口测试通过临时脚本进行，尚未沉淀成独立测试命令。

## 8. 下一步实施顺序

后续目标是最小要塞定位闭环，不先扩展全部生存技能：

1. 统一配置和启动：加载模型/API/账号配置，移除硬编码依赖；启动失败非零退出，明确登录和会话归属。
2. 建立真实工具注册/分发：状态、背包、采眼、计算、受监控移动、停止；先验证只读调用和保存观测回放。工具名、schema、skill 调用表保持一致。
3. 接通 AgentLoop：加载当前精简 skill，构造少量状态/任务摘要，校验 tool_calls 参数，执行并回传 role=tool 结果。一个普通动作在途，保留独立观察和停止通道。
4. 完善程序边界：按世界隔离投眼记录、保留真实结果及失败路线；控制上下文长度；模型重试与动作恢复分开；记录日志和后置条件。
5. 再验证真实采眼与短距离移动。统计误差优化先完成独立校验，导航需覆盖河流、卡住、超时、中断恢复；不能用假成功技能充当验收。

需要先说明并解决的缺口：缺少采眼等工具绑定、状态摘要与 skill 加载；旧客户端路径和移动超时字段；模型服务间歇故障；动作安全监控；误差模型尚未标定。主架构文档尚未按最新方向重写，阅读时以实现和本交接区分现状。

## 9. 跨机器准备与文件索引

使用 Python 3.11；当前运行依赖包括 requests、mcrcon，尚无统一依赖清单。官方计算桥接需 JDK 11+，保证 java/javac 可发现。创建各自虚拟环境，勿复制原机器的环境或已编译缓存。

以下命令在仓库根目录运行，`python` 指接手者自己虚拟环境的解释器：

```shell
python -m pip install requests mcrcon
python -m integrations.ninjabrain setup
python -m integrations.ninjabrain calculate --input data/examples/stronghold_throws.json
python -m unittest discover -s tests -v
python -m compileall -q main.py agent core skills debug integrations tests
```

第一次 setup 需下载官方 JAR，后续计算可离线。原环境安装曾遇 pip 初始化及 PyPI TLS 问题，属于当时环境现象，不是每台机器都需照搬的步骤。外部 API 与 Minecraft 的地址、白名单账号、认证口令及存档由部署者配置；回环地址只代表执行进程所在机器，不代表远程游戏服务器。

| 仓库相对路径 | 用途 |
|---|---|
| `AGENTS.md` | 贡献指南与小模型 skill 编写要求 |
| `agent/brain.py`、`agent/loop.py`、`agent/tools.py` | 模型请求、待接通循环、工具 schema |
| `core/client.py` | 游戏 API 客户端及已知适配缺口 |
| `integrations/ninjabrain/` | 官方计算桥接、采样器、CLI |
| `skills/stronghold.py` | 纯计算程序技能 |
| `skills/stronghold_localization/SKILL.md` | 游戏 AI 的精简策略 |
| `skills/stronghold_localization/references/research.md` | 误差分析、导航依据与实现边界 |
| `tests/test_stronghold.py` | 24 项最近通过的回归用例 |
| `docs/STRONGHOLD.md`、`docs/SKILL_SPEC.md` | 用法及接口契约 |
| `third_party/ninjabrain/` | 上游来源与许可证 |
| `probe_api.py` | 原始探测脚本，含已知速度字段误判 |

本次收尾前，集成、测试和部分新文档仍包含未跟踪文件，已有文件也存在未提交修改；没有创建本轮提交。**交接文件不等于代码已发布：传递工作时确保新文件一起纳入提交或补丁，并检查接收端文件完整性。** 私有配置、日志、JAR 和缓存默认不会传递；本文中的真实世界坐标仅作历史证据。

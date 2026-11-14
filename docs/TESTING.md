# 配置、客户端、睡眠与初期生存验证

验证日期：2026-11-13（Asia/Shanghai）。

## 22:52 寻路与模型反馈修复暂停点

- `python -B -m unittest discover -s tests -q`：293 项通过，4.456 秒；输出保存在忽略目录 `.test-runtime/navigation-offline-tests.log`。
- 当时实验性的 API 补丁通过了 13 项导航测试与语法检查；**2026-11-14 按用户要求已全部撤回，未提交或推送，当前不再包含该测试文件**。仅保留 BotSleep 侧适配和回归测试。
- 真实运行已确认 `default` 返回 `model=qwen3.6-35b-a3b`，模型调用了 move_to/gather_block，失败结果已进入反馈。原有寻路多次 stalled；原地 range 内移动未产生位移；历史膨胀后的请求实测 51,524 tokens 超过服务 43,776 上限并返回 400。
- 实际地形读取确认 `[6,112,-4]` 原木从机器人 `[4.3,109,-5.5]` 几何可触达但被土墙遮挡。修复增加可见性、站位候选、结果诊断与上下文预算；当前测试覆盖这些回归，**尚未完成修复后的真实自主采集、制作验收**。
- 用户准备断电，停止后续真机测试。机器人、监控和 API 已停；Minecraft 保存全部维度后正常退出。下次须由用户明确恢复，再使用原版 API 与机器人观察真实库存增长；原版 API 未提供的视线等元数据仍属观察限制。

## 离线自动化回归

```powershell
python -B -m unittest discover -s tests -v
```

前一轮睡眠模块验收结果：**101 项通过**。后续生存新增回归与真实验收记录见本文后续章节。原测试包括：

- 配置文件、环境变量与 CLI 优先级、类型和范围校验、凭据不回显。
- API 路由、响应结构、业务错误、网络超时、不重发写动作、出生前暂态就绪错误。
- 床头脚去重、占用/消失/不可达床、危险维度、怪物、死亡、取消、超时。
- 入睡和醒来证据，单人瞬时跳夜、多人等待、起床包先于时间或天气包到达。
- 单任务调度、停止后暂停、线程清理、聊天名单与格式解析。
- 配置到主入口的真实 HTTP 请求链：测试连接本机临时假 API，验证成功、无床和登录失败后的退出及清理。

离线测试不连接游戏、不调用模型、不执行 RCON。29 个 Python 文件也通过 AST 解析；`git diff --check` 通过。正常入口能在没有 mcrcon 的环境中导入和运行配置检查。

运行环境：Python 3.10.9、requests 2.33.1、PyYAML 6.0.2。离线结果保存在本地忽略目录 `.test-runtime/offline-tests.log`。

## 独立真实游戏验收

本轮使用 `.test-runtime/leaves` 内的独立世界，复制原目录的服务端二进制和依赖，不使用硬链接，不修改原服务器配置或存档。

| 项目 | 配置 |
|---|---|
| 游戏/服务端 | Minecraft Java 1.21.11 / Leaves |
| Java / Node | Java 24.0.2 / Node 24.20.0 |
| 游戏地址 | `127.0.0.1:25585` |
| 测试 API | `http://127.0.0.1:18920/api/v1` |
| 测试 RCON | `127.0.0.1:25595` |
| 世界 | `botsleep-test-world`，独立超平坦世界 |
| 测试机器人 | `SleepTestBot`，offline + direct |
| 测试平台 | 主世界 `(0,100,0)` 附近；红床位于 `(3,100,0)` / `(4,100,0)` |

API/RCON 使用独立随机口令，保存在被 Git 忽略的测试配置中。测试脚本检查隔离目录、端口、世界、机器人和进程身份，拒绝指向其他环境。

以下为历史测试命令。**用户随后将 25585 服改成了正常生存世界，现已不再是可破坏测试场地；该脚本会因世界名不符拒绝运行，请不要修改校验绕过。** 恢复专用旧测试世界后才可重现：

```powershell
python scripts/live_sleep_smoke.py --runtime .test-runtime
```

此脚本**会改动独立测试世界**：布置平台、设置午夜、放置/移除床，并启动真实 `main.py --once`。不要在用户正在查看或操作测试世界时运行。

最终三个场景均通过：

| 场景 | 结果 | 游戏内证据 |
|---|---|---|
| 夜间有床，第 1 次 | 主入口退出 0；`observed_sleep_then_wake` | RCON 观察到真实睡眠，时间 `18000 → 18` |
| 夜间有床，第 2 次 | 主入口退出 0；`observed_sleep_then_wake` | RCON 观察到真实睡眠，时间 `18000 → 20` |
| 夜间无床 | 主入口退出 1；`no_bed_nearby` | 未观察到睡眠，时间 `18000 → 18040`，没有跳夜 |

每次运行后机器人均自动登出。最终脱敏报告：`.test-runtime/live_sleep_report.json`；技能证据日志位于 `.test-runtime/logs/`。

首轮曾暴露“起床包先于时间包”的竞态：实际已经跳夜却误报中断。已补有界醒后确认窗口及回归测试，以上两次成功均来自修复后的源码；首轮报告保留为 `.test-runtime/live_sleep_report.initial-failure.json`。

用户随后要求进入查看、启动机器人并改成普通世界。当前 `.test-runtime/leaves/botsleep-survival-world` 为新随机种子 normal 世界，默认 survival、normal 难度、结构生成开启；原 `botsleep-test-world` 及下界/末地目录完整保留。Ink_bai 的 OP4 保留，未搬移旧玩家数据。生成器已经通过新 level.dat 确认是 `minecraft:noise` / `minecraft:overworld`，并非仅改 properties 的文字。迁移记录在 `.test-runtime/world_migration_report.json`，进程在 `.test-runtime/processes.json`。

## 验证范围

真实游戏验证了登录、正确床位查询、接近、交互、入睡、跳夜、醒来确认、无床失败和登出。

取消、危险维度、怪物、多床、断线、延迟状态包等边界已由离线测试覆盖，本轮没有逐一在游戏中制造这些场景；没有验证长期生存、多人真实睡眠、插件自定义聊天或模型决策。

同步 HTTP 请求无法被 Python 取消信号即时打断；后端 `/stop` 也不保证所有动作立刻停止。当前控制器会停止后续动作，等待请求返回或超时，并在退出时登出。

## 初期生存新增验证（同日）

最终源码 **197 项 unittest 全部通过**。新增覆盖六个生存动作的真实结果判断、材料扣除的规划模拟、参数/危险门禁、单动作互斥、执行前持久化意图、未知结果暂停与重启不重放、死亡回退与延迟包、扫描中死亡导致快照失效、手动请求忙态保护、步骤/运行时间预算、断线复用会话、世界隔离与损坏存档保护。37 个项目 Python 文件通过 AST 解析，`git diff --check` 通过。

真实游戏测试另建 `.survival-test-runtime`，使用 **25586 / RCON 25596 / API 18921**，账号 `SurvivalTestBot`，世界 `survival-smoke-world`。这是白天、和平难度的可破坏平坦测试场，泥土地板、24 个原木和24 个侧面石块。机器人从空背包开始，不授予物品；工作台必须自行合成并放置。运行真实 `main.py --mode survival --max-steps 80`，完成后以服务端保存的玩家 NBT 背包独立验算目标物品。

| 运行 | 结果 | 证据与恢复 |
|---|---|---|
| 首次预检 | 登录阶段失败，没有进入采集 | 暴露出生前 status 的精确 toFixed 初始化错误；客户端现于原登录 deadline 内重试该暂态，不泛化重试 internal |
| 初次完整流程 | 41步，120.92秒，退出0 | 石镐、石斧、石剑、熔炉各1；4次接近失败与1次未收集到掉落，调度器自行换目标，无补发材料 |
| 收紧寻路半径后的最终源码 | **38步，99.86秒，退出0** | 四件目标各1，工作台本轮自制自放；原接近失败点直接成功，1次禁止挖自身上下列的保护拒绝后自行换目标完成 |

寻路修复将接近半径从3缩到2，保留实际距离不超过3.5的判断。原因是上游 GoalNear 按整格节点判断距离，合法终点的实际脚坐标可能超过技能交互门槛；未放宽成功标准。

完整报告：`.survival-test-runtime/live_survival_report.json`；每轮历史保存在带时间戳的同名前缀文件，stdout/stderr、决策和检查点也保留。两次成功后机器人均自动登出；专用 Java/API 已实际退出，25586/25596/18921 已释放。最终只读确认25585普通世界仍有 SleepTestBot 与 Ink_bai 在线，Normal 难度、Ink_bai OP4 保持。

脚本必须显式运行，不属于 unittest：

```powershell
# --prepare 仅允许不存在的新目录，拒绝覆盖旧场地。
python scripts/live_survival_smoke.py --runtime .survival-test-runtime --prepare
python scripts/live_survival_smoke.py --runtime .survival-test-runtime --start
python scripts/live_survival_smoke.py --runtime .survival-test-runtime --run
python scripts/live_survival_smoke.py --runtime .survival-test-runtime --shutdown
```

脚本固定校验目录、端口、世界、账号和进程所有者；不要绕过这些限制指向用户世界。现场测试依赖当前 Windows Java/Node 安装、相邻服务端缓存与 mc-agent-api 的本地依赖。

**本次没有验证普通自然地图的长期自主生存**。食物获取、熔炼、建屋、战斗尚未实现；饥饿/撤退/死亡恢复等边界主要由离线测试验证，不能将上述工具链测试视为这些能力的真实游戏验收。

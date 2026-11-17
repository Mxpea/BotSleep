# 配置、客户端、生存、赛事与监控验证

## dev-ink 同步后的住所与撤退修复（2026-11-17）

`main 9018f76` 已快进同步到 `dev-ink`，原普通世界 `botsleep-survival-world`、Leaves 1.21.11 和官方 API 1.3.14 用于本轮实服检查。只改 BotSleep；没有给予物品、传送、调整时间/难度或制造测试地形。

本轮修复施工站位被目标程序当成非资源坐标丢弃、`needs_position` 误冷却整个基地、检查住所时丢失站位需求，以及最后屋顶站位到门口的路线穿墙。住所材料与 `StructureSpec` 共用支持集合，已有且核验为打开的门优先关闭。离线假世界由真实 `ShelterSkills`、`PositioningSkills`、`DailySurvivalSkills` 执行，四朝向均完成墙顶与关闭的门；备齐材料的北向案例使用 55 次逐块施工、16 次站位和 1 次放门，共 72 个本地子步，没有 AI 升级。这些是离线执行证明，不是实服造屋完成。

空背包的混合木材回归另发现门材死锁：5 桦木板加 4 橡木板被通用木板总量判断成已满足 6 板配方，实际无法合成任一木门。现在按具体木种补齐门材，采木子步骤也保留该木种需求。库存守恒假世界中，纯橡木链 110 步、混木链 112 步完成；分别采 17/18 根原木，施工消耗 55 板、工作台 4 板、门配方同种 6 板产 3 门（放 1 留 2），最后分别剩 3/7 板。采集/合成由假世界模拟，施工、站位、桌子放置和门使用真实技能；这不是 600 秒内完成的实服保证，也不代表 shelter 目标会强制先做石器工具。

撤退新增树冠下降时对已在危险圈内、始终低于脚底 2.5 格以上的次要射手进行水平不接近判断；圈外禁入、同高度/近战限制及执行前复核仍保留。实服采集的 647 格地形保存为 `tests/fixtures/live_retreat_nine_hostiles.json`，未采集区域按 unknown 处理。落叶层只允许触发相邻方块接触检查，绝不作为实心支撑；须证实实际脚印重叠、支撑顶面高度和净空，并在移动前重新读取。受伤、新敌进入危险圈或已有敌明显逼近时，旧的撤退搜索冷却失效；无变化时仍有 30 秒搜索冷却，近射手时每秒观察一次。未知动作不会因受伤而重放。

实服会话 `be5c7a42a46f`（本机日志 `.test-runtime/daily-20261117T063055Z.stdout.log`）记录 17 次已验证移动，其中 15 次属于撤离中继；曾从树冠走到草地。该轮没有模型请求，也没有采集、制作或建造产出。服务器随后确认 14:32:48 被骷髅逼落、14:33:10 被掠夺者射杀、14:33:46 被骷髅射杀，触发上游死亡冷却并断开。这一轮暴露了落叶边缘误判和受伤不提前结束冷却的问题，不能把自然重生或中继移动算作稳定生存通过。暂停结果现在直接发布 `phase/pause_reason`，即使断线后心跳失败，监控也能显示正确原因。

边缘回退另支持有已验证邻块接触时，直接下降到同列低一格的实心落点，分别检查上层水平段和竖直下降段的净空、所有敌人的最近距离，并重读源支撑与目标，最后验证真实落地。独立下降只算局部恢复，不自动算完整撤离。复核发现无敌 `canopy_escape` 原有首尾威胁检查不足以授权新下降，已将该新增能力限制为 `retreat_from` 并保留反例测试。

中间版本完整离线回归为 **1478 项 / 103.163 秒：1473 通过、5 项实服专用跳过**；加入受伤中断适配后为 **1492 项 / 102.785 秒：1487 通过、5 项实服专用跳过**。命令为 `$env:BOTSLEEP_RUN_LIVE_TESTS='0'; python -B -m unittest discover -s tests -q`。这些数字早于后续实服反馈修复，不能当作最新源码或实服通过证明。

14:50:48 启动的下一次实服尝试执行了 13 步，其中 3 次已验证下坡移动；14:51:04 在下一次移动中被骷髅击杀，动作保留为 `action_unconfirmed`，暂停后没有重放旧动作。14:51:13 上游再因死亡保护断开。此轮仍没有采集、制作或建造产出，暂停原因已能在断线监控中正确显示；后续恢复须等待上游冷却，并通过 fresh 重观测放弃旧动作意图，不能以重启绕过冷却或修改 pending 文件。

### 隔离普通地图验证（2026-11-17，Asia/Shanghai）

用户明确同意增加普通生成的隔离世界。原存档保留，新世界 `botsleep-shelter-validation-20261117` 使用相同 Leaves 1.21.11 / 官方 API 1.3.14，生存、普通难度；没有给物、传送、改时间或布置地形。游戏端口 `25586`、API `18921`、只读监控 `18931`，账号 `ShelterTestBot`。两个世界共享同一模型串行门禁，各自使用独立检查点和进程清单。

15:14 会话（`bot-20261117T071410Z.stdout.log`）第一次模型响应实际为 `qwen3.6-35b-a3b`，请求路由 `default`。`gather logs=8` 在 **9 个本地子步骤、2 次本地恢复、0 次 AI 升级**后得到 7 橡木原木和 1 桦木原木；随后工作台目标 2 步、木镐目标 7 步完成，包含两次工作台放置失败后换位置，实际工作台在 `[8,71,-11]`。另一采木目标又完成 4 根原木。该会话共 17 次模型响应，不能误说整轮只有一次模型调用；只有第一个采木长任务期间没有追加调用。

同会话还确认清除树叶并落到 y=69 后恢复生产。随后四次圆石目标均未产出：可见石头埋在 y=57–59，当前地面约 y=68，短距地表探索无法证明可达。已补充模型目标说明：木屋执行器负责木料、木板、工作台与门，不以石器、熔炉或燃料储备为前置，重复零产出应调整策略。

15:21 会话新增模型请求为 **0**；夜间完成 25 次撤退移动、11 次树冠恢复，但仍在 15:23:01、15:23:08、15:24:47 三次死亡，随后死亡上限暂停。复活满血不能当作无死亡。15:24:42 官方 API 明确返回因 hurt 终止，BotSleep 只读复核真实落点后报告 `retreat_interrupted`，随后重新观察并选择新撤退；没有追加 stop 或重放旧动作。这验证了已确认中断的适配，但不代表夜间避险已稳定。

15:33 自然白天新会话首次模型选择 `shelter`，真实执行四次只读选址；平坦地面仍因落叶/草判为占用且出口不通过，提前耗尽四次局部失败预算并请求 AI。当时没有施工、完整住所未通过实服验收。后续修复将只读候选否决与真正执行失败分开，并适配经过确认的可替换地表植被；最终结果需以之后的几何和库存证据为准。

原服另修复树冠落点记录：已访问落点再次到达时刷新最近顺序，阻止两格循环。64 步耗尽后只允许显式恢复申请一次新窗口；必须有新鲜完整、安全且同生命代/维度的观察，全局预算仍有余量，pending/unknown 会拒绝。保留排除点、访问历史、累计步数和窗口审计，不自动重启预算。

15:55–16:12 原服新窗口实测保留原 64 步累计值和 17 个访问落点，重新执行 64 次树冠恢复尝试；另有 19 次撤退中继成功，但仍未落地，最终正常耗尽窗口暂停。共 350 个本地安全步骤，新增模型请求和死亡均为 0，最终生命/饥饿 20/20。树冠最近落点去重生效，但多敌撤退仍出现回访，不能据此宣称所有寻路循环或任意树冠脱困已解决。原服已安全登出，监控保留维修状态。

15:55 隔离服新会话实测三次上升 1 格，均由官方移动 API 返回后独立确认稳定落点；合计 22 次定位成功，2 次发现树叶/坡地阻挡在写前拒绝，0 个移动 unknown。住所目标执行 82 次选址（71 个不同中心，11 次跨目标重复），没有合格的完整场地；期间 9 次串行模型响应，没有逐块问 AI。由此修复跨目标负向选址缓存，并加入内部天然土块准备链。16:00 一次采木虽然方块已移除，却未拾取下坡掉落，正确报告 `gather_unverified`；没有把挖掉当作拿到原木。黄昏制作被本地安全规则拦截，16:01 安全登出，生命/饥饿仍为 20/20。

上述台阶与选址版本完整回归为 **1553 项 / 166.383 秒：1548 通过、5 项实服专用跳过**。之后新增的负缓存与土块准备有各自专项回归；土块准备先独立读计划、每步最多一挖、随后重新勘测，不能把离线“准备→首块施工”通过当作实服已建屋。最终全套和实服结果另补，不混用此中间版本数字。

加入跨目标/重启负缓存、内部天然土块准备及其调度后的完整回归：**1592 项 / 160.881 秒，1587 通过、5 项实服专用跳过**，日志为本机 `.test-runtime/shelter-preparation-final-regression-20261117.log`。命令仍为 `$env:BOTSLEEP_RUN_LIVE_TESTS='0'; python -B -m unittest discover -s tests -q`。172 个受版本管理或待提交 Python 文件 AST 通过，示例/隔离配置有效，`git diff --check` 通过；私有配置、世界、日志没有纳入 Git，mc-agent-api 跟踪源码未修改。准备技能 19 项专项另经独立复审；真实快照缺失邻格时正确拒绝，仍须自然白天补读并做实服验收。

最近合并复核：2026-11-17（Asia/Shanghai），`main 02343f9` + `develop-tianya/shelter-workflow 39afec8`，在独立 main 工作区合并。下方两侧分支的历史日志与实服记录保留，不替代本次合并验证。

## shelter-workflow 合并取舍与回归

| 重复或交叉部分 | 合并后的实现与理由 |
| --- | --- |
| 住所规划与施工 | 采用共享 `StructureSpec` 四方向蓝图；保留 main 的单步/有界批量施工、真实库存和几何验收。默认 north 兼容旧检查点，床/箱子/入口随方向旋转，方向写回 `base_project` 并在下一轮重新观察。 |
| 领域常量、注册和退避 | 采用 `core.domain`、`SkillRegistry`、`RetreatController` 去重；完整保留自主目标、战斗、桶类和树冠/觅食恢复。starter/daily/competition 仍为 9/35/40，危险目的地检查不误拦截战斗目标。 |
| 模型与长任务 | 保留 main 的一次模型授权、多子步执行、按任务收窄修复目录及串行模型门禁；独立深矿/洞穴工作流没有自动接管 daily。 |
| API 与实例管理 | 保留 API 1.3.14 断线未知语义和原参数兼容；吸收实例锁、只读 `botctl`、显式会话复用。只读重连最多一次并共用原截止时间；开窗和写入不重试。competition 默认仍登出。 |
| 结构/矿井/门户未知结果 | 未知写入跨子流程立即返回，不能换站、新建 guard 返程、追加搭柱或姿态清理；有 journal 的矿井入口须保存动作意图，未知结果阻止自动续跑。 |
| 容器与导航 | 独立容器保留批量能力，四公开入口统一返回未知 `SkillResult`；开窗按副作用记账，缺数据不当空，没有足量产出不宣称熔炼完成。导航安全中止不作为几何障碍吞掉，返程须实际到达。 |

新增回归涵盖旋转住所 native call→检查点→新观察、主线战斗反馈与即时退避、实例/退出默认、客户端超时预算、工作流跨层未知传播、容器真实产出及两项洞穴实服测试的离线隔离。首次全套发现战斗目标被误当生产危险目的地拦截，已修正而非放宽原有测试断言。最终完整回归 **1425 项 / 124.967 秒：1420 通过、5 项实服专用跳过，`OK (skipped=5)`**，命令为 `$env:BOTSLEEP_RUN_LIVE_TESTS='0'; python -B -m unittest discover -s tests -v`。159 个 Python 文件 AST、两个入口的示例配置检查及 `git diff --check` 均通过。

新增分支的离线矿井/洞穴测试原先会创建默认 `MemoryEngine` 并写入受跟踪的历史记忆文件；合并时改为显式注入测试替身，生成的记录已恢复，未纳入提交。全套通过后仅调整这三份测试的记忆依赖注入，相关 41 项用例另以默认记忆构造器拒绝调用的方式验证隔离，全部通过。

本次仅离线测试及本地假服务，不登录游戏、不请求真实模型，不使用 RCON、不更改原存档。洞穴/门户实服用例显式跳过。新分支中的独立 3D 记忆和缓存不代替主线新鲜观察；持久化应注入包含服务器、账号、world_id、dimension 的 `VoxelWorld3D.for_runtime(...)`，便利参数的默认作用域不能作为跨世界隔离保证。普通地图下的深矿返程、完整建屋与完整生存仍需后续实服验收。


## main 分支历史验证

最近复核：2026-11-16（Asia/Shanghai），源码基线为 `dev-ink 402cc50` 上的当前未提交修改，包含目标程序、动态模型工具路由及既有狩猎/战斗改动。进程状态只对下述采样时间有效。

## 树冠与骷髅避险的实服回归（2026-11-16，后续轮次）

本轮继续使用原普通世界、官方 API 和串行模型门禁，只修改 BotSleep。修复包含：脚下支撑的新鲜观察、逐步树冠中继、原木顶部识别、单叶开口、最多 1.5 秒的只读落稳确认、多敌人退避、低血量觅食死锁与受限生产目标，以及 `operator_goal` 和对应动态请求目录。API 的死亡标记可能为空，因此本地生命序号也参与恢复证据失效处理。

17:19 会话的真实日志中有 35 个非等待动作、1 次成功清叶、8 次经落稳确认的移动，`LLM_REQUEST=0`。其中 17:21:43 清除 `[5,121,5]`，下一轮到达 `[5.5,121,5.4]`，生命/饥饿保持 6/17；这证明了内部清叶和重新观察后的移动，没有证明已经到达地面。随后服务器于 **17:25:36** 记录 `SleepTestBot was shot by Skeleton`，暴露单敌方向策略会靠近另一射手的问题。未知动作保持 pending，开发期间机器人登出，监控继续运行。失败证据没有删除，也不能把自然重生的满血当作成功避险。

之后补上多敌退避、上台阶出口、生命序号失效、威胁消失后的完整观察与恢复、监控状态新鲜度及生存主循环调度。最终离线检查为 **1263 项 / 27.899 秒，1260 通过、3 实服专用跳过，`OK (skipped=3)`**，命令为 `$env:BOTSLEEP_RUN_LIVE_TESTS='0'; python -B -m unittest discover -s tests`；128 个 Python 文件 AST、示例配置与 `git diff --check` 均通过。两份真实地形提炼为 `tests/fixtures/canopy_progressive_exit.json` 与 `canopy_trunk_exit.json`，离线地形可达性不代替实服当前视线、敌人和库存证据。

后续 17:47 会话仍发生蜘蛛和骷髅击杀，API 的 5 分钟死亡循环冷却没有绕过；17:57 会话暴露健康状态下仍沿用短观察、无法确认威胁消失的问题。修复后 **18:02:49** 读到脚下 `stone [-3,115,9]`，记录 `RETREAT_THREAT_CLEARED mode=solid_footing` 并恢复正常调度。这个地面状态发生在先前自然重生之后，不能算作从最初树冠无伤脱困。

**核心生产验收（18:09–18:10）**：`default` 路由只请求一次，实际响应 `model=qwen3.6-35b-a3b`，授权 `run_survival_task(task=craft,item=wooden_pickaxe,count=1)`，任务 `task-1794823753312-1`。程序完成 **12 个子步骤（10 成功、2 已知失败）**：实际采集 3 根橡木原木，制作木板、木棍和工作台；两次工作台放置被判不安全后换候选，在 `[-7,119,9]` 放置成功，最终制作木镐。18:10:00 记录 `TASK_COMPLETED`，`local_recoveries=2`、`ai_escalations=0`。新鲜库存为木镐 1、木板 3、木棍 2，生命/饥饿 20/20。目标执行期间 **0 次额外模型请求**，原始 call ID 仅有一次工具结果和一次任务反馈；下一次正式请求带上该配对结果且模型正常响应。木料数量按木镐及工作台前置需求计算，没有测试采集 16 根或更大批量。

后续模型选择圆石 8，程序先完成 8 次有界探索，仍未找到安全采集候选才报告 `needs_ai`。修复规划当时因全量工具目录导致 52580 字节超过 48000 上限，**没有发送 HTTP 请求**，最终暂停。现已按任务与新鲜证据收窄原子修复目录；预算回归中相同上下文由全目录 48188 字节减至 30161 字节，仍保留当前状态和最近原生反馈。126 项相关测试通过并已重启加载；最新会话处于夜间本地等待，实际修复模型请求仍待后续白天验证，不能说圆石目标已经完成。

本机忽略目录中的原始证据：`.test-runtime/daily-20261116T091915Z.stdout.log`、`tree-recovery-validation-20261116.json`、`canopy-live-second-watch.json`、`canopy-live-third-watch.json`；生产日志为 `daily-20261116T100817Z.stdout.log`，独立汇总为 **`autonomy-one-request-live-20261116.json`** 与 `production-live-fourth-watch.json`。监控累计计数包含旧检查点，本次请求数按当前会话日志核对。最终源码还补齐了已发写入动作的 `executed` 标记；旧日志未回写，生产成功以实际库存与方块变化为证据。

本轮仅为传达用户验收目标而新增私有配置的 `survival.operator_goal`，模型凭据与服务地址未改变；相邻 mc-agent-api 跟踪文件及原有 lockfile 未修改。没有给予物品、传送、调整世界时间/天气、召唤实体或用管理命令布置地形。机器人正常采木、挖路和制作产生的变化保留在原存档中。18:23 采样，最新机器人已加载修复、满血满饥饿持有木镐，监控继续开启；此前夜间死亡记录不被本次生产成功覆盖。

## 目标程序与减少模型调用（2026-11-16）

`run_survival_task` 已连接原生模型协议、持久化检查点、逐步执行与实时面板。默认正常 daily 请求只有 6 个目标/安全工具，competition 为 11 个；完整能力目录仍为 9/35/40。目标遇到 `needs_ai` 时最多接受两次生产原子修复提案，之后收回该授权，不能因旧失败快照长期退回逐步模型规划。详见 [AUTONOMOUS_TASKS.md](AUTONOMOUS_TASKS.md)。

| 检查 | 本轮结果 |
|---|---|
| `$env:BOTSLEEP_RUN_LIVE_TESTS='0'; python -B -m unittest discover -s tests -q` | **1090 项 / 29.281 秒：1087 通过、3 实服专用跳过，`OK (skipped=3)`**。 |
| 目标控制器 | 35 项专项：4 类资源、常用配方前置、熔炼等待/收取、换目标/清叶、有限探索、未知结果、生命周期与终止边界。 |
| 真实 harness/skills + 内存游戏 | 17 项专项：混合木头净增 4 只调用一次模型并最终配对一次反馈；空背包到木镐；炉子连续等待无重复投料或模型请求；路径失败换目标；进食/撤退抢占恢复；unknown/restart 不重放；全局预算/死亡上限结清反馈；最后允许子步恰好完成时仍如实 completed。 |
| 模型工具路由 | 21 项专项：正常日常 6 / 赛事 11 个实际请求工具；隐藏的原子提案拒绝并有界格式修复；每目标最多两次原子修复；unknown 不解锁；关闭 autonomy 保留单步模式。旧原子协议测试显式关闭 autonomy，新目标集成保持默认开启。 |
| 监控 | 新增 8 项专项；任务快照、未知值、脱敏、重复事件与实际页面脚本渲染覆盖。没有浏览器视觉验收。 |
| 配置与语法 | 示例配置有效；当前 117 个 Python 文件 AST 解析通过；`git diff --check` 通过。 |
| 修改范围 | 仅 BotSleep；相邻官方 API 跟踪文件无修改，原未跟踪 lockfile 及私有模型配置 SHA-256 与本轮前一致。 |

内存游戏测试会实际扣除材料、放置工作台、改变方块/背包/熔炉槽位，不以固定成功回包替代产出；这些仍是离线证据，不等于 12 类目标均已在真实普通地图验收。

实服首轮验证发现仅在提示词中建议长任务仍会被模型忽略：15:36 的进程继续返回 `move_to/clear_foliage`，低血量清叶被正确拒绝。因此增加按阶段限制实际工具目录及当次解析白名单，安全停止旧机器人后于 **15:44** 重新启动新版；仍使用原世界、官方 API 与本机只读监控，没有为测试给予物品、传送、改时间/天气或生成地形。最终实服观察见下方补充记录。

### 新版实服观察：15:44–15:50

最终进程使用新动态工具路由启动；到 **15:50:03**，真实只读监控完整保留本次会话事件 `seq=1..459`，累计完成 **54 个本地循环，新增 `LLM_REQUEST=0`**。这一时段实际处于夜间/近敌应急，不是采木目标。日志证实一次撤退移动（约 0.8 格、relay 未完成），随后被僵尸击杀并自然恢复；此后树冠周边安全落点不足，程序有界搜索、冷却和复查，没有调用模型重复规划退路。

白天采样：机器人 `[-4.1,124,5.7]`，生命 10、饥饿 17、附近骷髅约 7.1 格，`ready/alive=true`、未暂停、`pending=null`。服务器/API/机器人/监控保持运行，游戏 `127.0.0.1:25585`，面板 `http://127.0.0.1:18930`。没有后端异常输出。**本轮实服证实本地应急不依赖模型；没有证实新版生产长任务实际采集或制作成功。** 树冠路线与附近威胁仍是实服阻碍，不能用离线产出替代这个结论。

面板 `model_planning_rounds=5` 是旧检查点累计值，不是这次登录新增请求数；本次事件序列中请求确为 0。模型实际名称在 15:36 的正式响应中自报 `qwen3.6-35b-a3b`，新进程尚无模型响应，不能当作新进程重新核验。最终 HTML 累计计数说明调整后补跑 8 项任务监控专项通过（0.194 秒）。本机脱敏证据在 Git 忽略的 `.test-runtime/autonomy-live-final.json`、`autonomy-live-summary.json`，进程及日志路径见 `.test-runtime/processes.json`。

## 此前实服启动：日常生存，不以末影龙为目标（2026-11-16 14:57–15:06）

用户明确授权真实服务器测试。沿用 `.test-runtime/leaves` 中原 `botsleep-survival-world` 普通地图，Leaves Java 1.21.11，生存/普通难度；游戏 `127.0.0.1:25585`、官方 API 1.3.14 `127.0.0.1:18920`、只读监控 `127.0.0.1:18930`。没有更换存档、给物、传送、改变天气/时间或召唤实体。无需末影龙完成条件，配置为 daily + intent=survival。

实际登录 `SleepTestBot`，站在树冠 `[4,124,6.6]`，生命约 19.33、饥饿 17、空库存。模型槽检查为空闲后只通过 harness 发请求；10 次正式响应自报 `qwen3.6-35b-a3b`，请求路由 `default`。没有旁路模型探测或并发请求。

实测发现：模型反复选择 `clear_foliage(oak_leaves,[4,123,7])`，经一次修复仍重复。该坐标仅来自雷达类型汇总，不在当前 `blocks` 候选中，且低于脚部；本地拒绝正确，**没有执行这次清叶**。修复 `agent/llm_planner.py`，将汇总明确输出为只读 `terrain_summary`、代表位置命名 `nearest_pos`，保留数量/距离与真实 blocks/站位候选；错误反馈解释授权来源和下方层限制。没有放松挖掘门禁。新增 2 项回归，80 项定向测试通过；随后全套 **1007 项 / 23.934 秒，1004 通过、3 实服跳过**。

15:05 在模型槽 idle、后端动作空闲时替换机器人进程，未清模型锁或重放动作。新进程已重新登录，监控恢复，仍为 daily 生存、未暂停、pending=null。截至 15:06，处于夜间本地等待/撤退复查；新进程尚未发送生产规划请求，**不能说上述提示修复已在真实生产行动中奏效**。

三次初始退避都没有发出移动，`target=null`、`solid_nodes=0`；首次 33 节点/248 方块读达到预算，后两次 15 节点/111 与117读仍无安全落点。这不是已发移动的寻路超时，也不足以证明整棵树无路。当前算法需先证明完整地面路线才走叶顶中继；真实可安全下树路径尚未验证。**采集、制作、狩猎、战斗和岩浆回收均未在本次普通世界验收通过。** 服务保持运行，后续需继续观察或补足地形证据。

本地脱敏证据（均被 Git 忽略、不随拉取分发）：`.test-runtime/live-daily-20261116-observer.json`、`live-20261116-before-prompt-fix.json`、`live-20261116-after-prompt-fix.json`；进程及日志位置更新于 `.test-runtime/processes.json`。其中 PID/健康状态是采样值，使用前必须重新核对。

## 离线基线：狩猎、战斗、液体操作与阶段建议（2026-11-16）

环境为 Windows、Python 3.10.9、requests 2.33.1、PyYAML 6.0.2、JDK/javac 24.0.2；沿用已校验的 Ninjabrain 1.5.2 缓存。本轮不安装依赖、不启动游戏/API/机器人，不调用真实模型或 RCON。只有离线替身、临时本机假 HTTP 服务与 Java 计算进程。

| 检查 | 本轮结果 |
| --- | --- |
| `$env:BOTSLEEP_RUN_LIVE_TESTS='0'; python -B -m unittest discover -s tests -q` | **1005 项，17.781 秒：1002 项通过、3 项跳过，`OK (skipped=3)`**。3 项为门户实服测试；要塞计算引擎用例实际执行。相对前轮 874 项增加 131 项。 |
| Python 语法 | 当前 113 个 Python 文件通过 `ast.parse`。 |
| 示例配置 | `python -X utf8 -B main.py --config config.example.yaml --check-config` 返回 `Configuration valid.`；默认 `survival.intent=survival`，非法 intent 拒绝。 |
| 监控 | 1 段页面脚本通过 `node --check`；全套含真实脚本离线 DOM 渲染、状态脱敏与只读接口测试。没有浏览器视觉验收。 |
| 修改范围 | 相邻官方 API 仍为 `37be500 / 1.3.14`，无跟踪文件修改；原未跟踪 `package-lock.json` 和私有 `.test-runtime/llm.yaml` 的 SHA-256 与本轮前一致。 |

新增验证重点：

- **23 项狩猎专项**：猪/牛/羊/鸡、原肉/熟肉库存证据，最多 4 击与内部 8 秒预算；移动动物使用最终位置瞄准，最新扫描中消失、超距、改身份、重复 ID 或饱和拒绝攻击。慢读/取消/断线后的未知写保持 pending。
- **31 项战斗专项及客户端合同**：有盾/无盾近战、弓弩单发、苦力怕只远射或退避；身体/射线/旁观实体检查、最后位置和时间预算复核。发射、伤害、击杀分开，连续无证伤害触发撤退冷却；盾释放独立记录。
- **18 项桶专项**：水/岩浆源核验、容量和桶数量变化、容纳坑/可燃物/旁观者拒绝、明确放置后的源回收、未知放置或回收不重放。测试替身模拟官方倒桶 reference/face 射线，原中心站位被坑壁挡住时零写拒绝，合法内缘站位才允许放置。
- **24 项阶段建议专项**：初期工具/食物、盾桶铁器、日常设施、速通前置与版本限制；只有已绑定技能产生候选，后段缺适配保持 blocked；最多 2 KB 模块结果，未知动作优先恢复。
- **主流程与监控回归**：模型和规则规划共用战斗撤退门禁，远动物/远羊先接近；既有生产失败接近熔断阈值时，已知 `combat_no_progress` 保留真实原生工具反馈并实际派发下一轮撤退，不被暂停吞掉。未知/fatal 仍暂停；短安全观察不授权生产，旧阶段提示标 stale；48 KB 赛事上下文预算、完整目录及反馈配对继续通过。监控不从成功或实体消失推导击杀。

这些结果不证明真实服务器已经击杀、格挡箭、回收岩浆或持续产出食物。新增能力尚未进行普通世界长时验收；苦力怕近战、自动繁殖/剪毛、下界导航/交易、自动绑定门户和要塞、末影龙战斗仍有明确缺口，见 [COMBAT_AND_HUNTING.md](COMBAT_AND_HUNTING.md) 与 [SURVIVAL_PLAYBOOK.md](SURVIVAL_PLAYBOOK.md)。

## 历史：官方 API 更新与 dev-ink 同步（2026-11-16）

相邻 `mc-agent-api` 按用户要求从官方 `1cfecc3`（1.3.7）快进至 `37be500`（1.3.14），无本地源码补丁。BotSleep 从 `8ae68e9` 快进至主线 `c53f70a`，保留主线新增的赛事任务、独立门户/合成/伐木/雷达及实服测试门禁。

环境：Windows、Python 3.10.9、requests 2.33.1、PyYAML 6.0.2、JDK/javac 24.0.2。固定 Ninjabrain 1.5.2 JAR 已校验；本轮不安装或更换依赖，不启动实际 API、机器人或游戏，不连接模型、RCON 或 Minecraft。监控 HTTP 测试只使用本机临时服务。

| 检查 | 本轮结果 |
| --- | --- |
| `$env:BOTSLEEP_RUN_LIVE_TESTS='0'; python -X utf8 -B -m unittest discover -s tests -q` | **874 项，16.446 秒：871 项通过、3 项跳过，`OK (skipped=3)`**。3 项均为门户实服测试；Ninjabrain 引擎/离线 CLI 用例实际执行。 |
| `python -m compileall -q main.py agent core skills debug integrations` | 通过。 |
| `python -X utf8 -B main.py --config config.example.yaml --check-config` | `Configuration valid.`；私有配置另以 `load_config(..., environ={})` 检查通过，文件 SHA-256 与更新前一致。 |
| 监控页面 JavaScript | 提取 `debug/monitor.html` 的 1 段脚本，以 `node --check` 解析通过；未做浏览器视觉验收。 |
| 官方 API `npm run check` / `npm ls --depth=0` | 源码语法与已安装依赖检查通过；跟踪文件无本地修改，原有未跟踪的锁文件保持不变。 |

新增 29 项回归验证本次同步发现的问题：

- **18 项 API 兼容回归**：新版独立 worker 可能在写动作执行后返回 `not_connected`；客户端和技能将写请求或写后验证断线标为 `action_unconfirmed`，保留原 API 错误与 pending，暂停且不补发动作或 `stop`。普通前置读取断线保持原语义；睡眠、清叶、住所、远处容器的接近移动和批量技能子步骤均覆盖。`dig(pos, False)` 的旧拾取参数与新版 `dig(x, y, z)` 均保留。
- **6 项赛事预算回归**：`--max-steps` 作用于当前 objective，模型收到的剩余步数/时间与实际执行门禁一致；一步耗尽退出 `2`，最后一步完成赛事仍退出 `0`。
- **5 项赛事上下文回归**：保留当前熔炉、容器、护甲、住所及证据；仅去除与原生工具结果完全相同的副本，并无损压缩 JSON。48,000 字节预算、125 项完整目录和最新工具事务不变；四轮离线请求为 44,367 / 45,791 / 47,384 / 47,270 字节，超限用例拒绝发送且保留反馈供后续使用。

主线提供 starter/daily/competition 9/31/36 个模型工具，赛事 45 个明确计分项目中仅 7 个有行动计划；独立门户、整树伐木、配方合成和雷达没有自动绑定到模型执行链。本轮离线通过不等于新增实服采集、制作、门户或完整生存验收。

下方 Python 3.11.3 的 SQLite 清理 ERROR 在本机 Python 3.10.9 未复现；本轮未修改该测试或记忆模块，不能据此宣称跨环境问题已经修复。

## 历史：拉取后复核（2026-11-15，3801be9）

## shelter-workflow 分支历史验证（原分支记录）

最近独立复核：2026-11-17（Asia/Shanghai），分支 `develop-tianya/shelter-workflow`。首节为最新状态与验证记录；后续各节为历史证据，不代表当前进程在线。

## 分层 Y 带地表下矿、防卡死回地表与掉落物/连续步态验证（2026-11-17）

继 Git 检查点 `2744ccc` 后完成主世界分层矿脉规划与地表-深层闭环加固（相邻 `mc-agent-api` 未作任何修改；`starter 9 / daily 31 / competition 36` 模型目录与既有契约保持不变）：① [`core/mining_targets.py`](../core/mining_targets.py) 新增 `ORE_SPAWN_Y_BANDS`、`target_spawns_at_y`、`unmet_spawnable_targets_at_y` 与 `recommended_mining_bands`（遵循 `coal` 仅 `Y=0..192` 下层不生成、`diamond` 在 `Y=-64..15`、`iron` 在 `Y=16` 峰值与全层分布）；② [`skills/mining.py`](../skills/mining.py) 扩展 `build_mine_shaft(..., target_ys=...)` 与 `mine_targets_from_surface`：单条连续阶梯贯通 `Y=28 -> Y=-54 -> Y=16`（若浅层独占矿物如 `coal` 已达标则跳过冗余 `Y>=20` 鱼骨支线；阶梯遇暗河/洞腔支持最多 10 格水平跨步绕行并避开已有 `trail` 轨迹；`_wf_prepare_entry` 改为先清入口头部与脚部方块再严格站位），新增 `_ensure_returned_to_surface` 与 `deep_retreat_done` / `_retreat_downsampled` 异常收敛护栏；③ [`skills/action_workflow.py`](../skills/action_workflow.py) 将 `escape_to_surface` 与 `pillar_up` 加固至 `max_ascend=160`（覆盖深板岩/矿石/壁炉火把自清理、落沙砂砾占格自挖脱困及 `tolerance=0.35` 烟囱单次迁移防抖动）；④ 新增离线回归 `test_ore_spawn_bands_and_stratified_planning`（[`tests/test_mining_targets.py`](../tests/test_mining_targets.py)）、`test_deep_band_exits_band_complete_when_only_shallow_coal_unmet_and_returns_to_surface` 与 `test_deep_retreat_done_prevents_re_descent_on_branch_retry`（[`tests/test_mining_workflow.py`](../tests/test_mining_workflow.py)）、`test_escape_to_surface_handles_wall_torch_coal_ore_and_falling_gravel`（[`tests/test_action_workflow.py`](../tests/test_action_workflow.py)）。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| 专项离线回归（`test_action_workflow` 6 项、`test_mining_workflow` 11 项、`test_mining_targets` 4 项、`test_world_3d` 8 项） | **29 项全部通过**（含 2 轮实测前缀 `2744ccc` 的 4 种地形捡矿及疾跑合并步态回归）。 |
| 全仓离线 `unittest`（最后一次 `.py` 代码修改后复跑，`$env:BOTSLEEP_RUN_LIVE_TESTS='0'`） | **963 项运行，OK（`skipped=10`，59.117s）**；跳过的 10 项为需本地 Ninjabrain JAR 的要塞用例、需连实服的 `test_speedrun_portal.py` 及 `test_cave_matrix.py`，不计作实服通过。 |
| `main.py --config config.example.yaml --check-config` + `compileall` | 输出 `Configuration valid.`，字节码编译全部通过；`git -C ..\mc-agent-api status -s` 无任何改动。 |
| **MCP 辅助真机排障与地表-深层闭环实测（`task-5561`，`[-159.5, 70, -859.5]` 出发）** | **通过（`success=True`，`stop=branch_done`，耗时 `848.6s`，`END POS=[-159.5, 70, -859.5]`，`on_surface=True`，`HP=20, Food=20`）**：先探浅洞并回到地表；随后开挖单条主斜井在 `Y=53` 提前采满 **`coal: 17/16`** 后于 `Y=28` 自动跳过冗余浅层支线直降至 **`Y=-54`**（沿途于 `[-56, -30, -860]` 挖获首颗钻石），在 `Y=-54` 完成左右鱼骨支线采矿并沿原阶梯直接疾跑步态走回地表起点，全程零漏捡与零死伤，净入包 **`diamond: 6, coal: 17, raw_copper: 16`**；此前对照轮次（`task-5427`）亦在 `[-130, 5, -893]` 一次性采齐 **`raw_iron: 17, coal: 17`**，并通过 `minecraft-debug` MCP 实测定位修复了 `Y=57` 暗河绕行、`coal_ore` / `wall_torch` / 落砂 `occupied` 阻断 `escape_to_surface` 等真机边界。 |

## 历史：通用 3D 空间世界记忆、A* 寻路、导航与采矿分层重构验证（2026-11-16）

继 Git 检查点 `615bba4` 后完成空间记忆与洞穴探索架构四层解耦重构（相邻 `mc-agent-api` 未作任何修改；`starter 9 / daily 31 / competition 36` 模型目录与既有公开契约保持不变）：① [`core/world_3d.py`](../core/world_3d.py) 提供可持久化的通用 `VoxelWorld3D`（chunk-zlib SQLite 存储、visited_stances、scanned_cells 前沿裁剪、open_frontiers、ore_registry 与 blocked_edges，严格遵循 Windows `contextlib.closing` AST 契约）；② [`core/pathfinder_3d.py`](../core/pathfinder_3d.py) 提供 `Pathfinder3D`（局部 horizon 扩展 + `find_path` 有向 3D A* 跨分支寻路 + `find_ore_vantage_path`）；③ [`skills/world_navigator.py`](../skills/world_navigator.py) 提供 `WorldNavigator`（统一移动/搭柱/搭桥/掉落执行、自主微掘清障、`blocked_edges` 记录与四级 `retreat_to_entry`）；④ [`skills/ore_harvester.py`](../skills/ore_harvester.py) 提供 `OreHarvester`（矿脉追踪、`collect=False` 实体提示踩捡及 cache invalidation 同步）；⑤ [`core/cave_probe.py`](../core/cave_probe.py) 与 `Pathfinder3D` 新增**锚定搭桥约束**（桥端必须落在真实实心方块前缘或安全矿点旁 2 格内，杜绝向悬崖虚空无限延伸天桥）与 `blocked_edges` 过滤，重构 [`skills/cave_explore.py`](../skills/cave_explore.py) 与 [`skills/mining.py`](../skills/mining.py)；⑥ 新增 [`tests/test_world_3d.py`](../tests/test_world_3d.py) 专项覆盖 SQLite 往返、A* 避障攀爬、锚定搭桥对比空洞零天桥，以及双分支矿洞穷尽采集与撤回。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| [`tests/test_world_3d.py`](../tests/test_world_3d.py) 离线专项 | **4 项全部通过（0.998s）**，验证 `VoxelWorld3D` SQLite chunk 读写与覆盖裁剪、`Pathfinder3D` 绕开 `blocked_edges` 与 2 格高台 `pillar` / 1 格台阶纯 `walk`、跨岩浆锚定搭桥 vs 悬崖虚空 0 天桥、以及 Y 型多分支矿洞铁+钻石全部采出并安全返回入口。 |
| 关联模块专项回归（`test_cave_probe`、`test_cave_explore`、`test_mining_workflow`、`test_contracts`） | **36 项全部通过（38.905s）**，包括深层鱼骨 30 铁配额、AST 无裸 `with sqlite3.connect` 检查与工具目录同步校验。 |
| 全仓离线 `unittest`（最后一次 `.py` 代码修改后复跑，`$env:BOTSLEEP_RUN_LIVE_TESTS='0'`） | **955 项运行，OK（`skipped=10`，61.015s）**；跳过的 10 项为需本地 Ninjabrain JAR 的要塞用例、需连实服的 `test_speedrun_portal.py` 及 `test_cave_matrix.py`，不计作实服通过。 |
| `main.py --config config.example.yaml --check-config` + `compileall` | 输出 `Configuration valid.`，字节码编译全部通过；`git -C ..\mc-agent-api status -s` 无任何改动。 |
| **大矿洞实服架构联调 A（`task-4780`，`[-99.3, -42, -982]`，`240s` 窗口，真实未重置地形） | 零失败安全返回（`stop=cave_done`，`HP=20, Food=20`，`END POS=[-99.4, -42, -981.5]`）：在 `VoxelWorld3D` 内累计记忆 **`30,594` 个体素**、访问 **`499` 处站位**（探索 `467` 格）、覆盖 `6,861` 格扫描空间与 `225` 处待探前沿，`551` 步移动单步耗时约 `0.21–0.49s`、**0 次向虚空凌空搭天桥（`PLACE=0`）** 并正确记入 `blocked_edges: 1`；因主区早先实测已采尽铁/钻，本轮记录附近红石脉为 `seen`。 |
| **大矿洞实服采矿捡矿联调 B（`task-4834`，`150s` 窗口，RCON 恢复 4 处原矿点后重测） | **通过（`stop=cave_done`，`HP=20, Food=20`，`END POS=[-99.5, -42, -981.5]`）**：`VoxelWorld3D` 记录 **`26,015` 个体素**、`219` 站位、`3,612` 格覆盖与 `158` 前沿（`blocked_edges=0`），先导航 17 步抵达 `[-64, -40, -985]` 挖出并踩捡 `[-66, -37, -985]` `deepslate_iron_ore`，随后自主垫脚搭柱 `PILLAR_UP` 至 `[-66, -39, -985]` 采收 `[-67, -36, -983]` `deepslate_iron_ore`，再跟踪寻路至 `[-61, -42, -991]` 连续挖取并实地踩捡 `[-59, -43, -992]` 与 `[-59, -43, -993]` 两块 `deepslate_diamond_ore`（实测入包 **`HARVESTED={'raw_iron': 2, 'diamond': 2}`，4/4 目标矿点全部 `mined`**），最后 5.12s 直跳返回入口并保持 `SleepBot` 在线。 |
| **大矿洞持续 30 铁矿目标实服联调 C（`task-4954`，`targets={"iron": 30}`，`150.3s` 一轮达标） | **通过（`stop=target_reached`，`HP=20, Food=20`，`END POS=[-99.5, -42, -981.5]`）**：全程共走 `123` 步依次收割并踩捡 5 组岩壁矿脉（挖出 `29` 块深板岩铁矿、净增 **`raw_iron=30/30`**），其中本地前沿清空后主动触发两次 `VoxelWorld3D` 记忆内跨洞 `Pathfinder3D` 3D A* 定向跳点（`23` 步与 `29` 步直达剩余已知未采矿脉，途中零兜圈、零多余搭天桥、`blocked_edges=0`）；配额达标后立刻中止后续盲探并在 9.26s 内直接返回入口，`VoxelWorld3D` 共记录 `16,974` 体素、`1,447` 覆盖格与 `83` 处后续可续探前沿。 |

## 历史：大型复杂矿洞 3D 连通穷尽探索、锚点边界与两级扩图验证（2026-11-16）

本轮根据 `-99.5, -42, -982.5` 深层大型多连通矿洞多轮实测定位到 `173` 格提前止步的根因，并完成两级 3D 穷尽探索、消搭高与踩捡捡矿闭环优化（相邻 `mc-agent-api` 未作任何修改；`starter 9 / daily 31 / competition 36` 模型目录保持不变）：① 通过 `4000` 节点全洞可达性探测（`task-4546`：`X=-111..-45, Y=-54..-25, Z=-1011..-929`）确认旧版在靠近 `explore_radius` 边界时 `frontiers[:16]` 会被越界节点占满，且局部 BFS 在支路死胡同处无法跨洞够到 40 格外的分叉口与上层缝隙；② [`core/cave_probe.py`](../core/cave_probe.py) 将邻接评估拆为“先自由步行（`dy in 0,+1,-1`）、后微掘/跌落/搭桥”消除 1 格台阶无意义 `pillar_up`，新增 `anchor_pos` / `anchor_radius` 硬边界过滤并将 `frontiers` 增至 32 个；③ [`skills/cave_explore.py`](../skills/cave_explore.py) 引入**按剩余读预算自适应的两级 3D 洪水扩图**（日常 ≤700 节点；局部前沿耗尽且 `remaining_reads >= 12000` 时在缓存内存中升级至最多 `4500` 节点跨洞重扫 `path[1:48]`，同时按 `remaining_reads // 12` 对共享小预算守护封顶避免 `MineShaftSkill` `max_reads=6000` 超限），新增 `_collect_mined_drop` 实地踩捡并将独立读预算提至 `150000/200000`；④ [`core/structure_spec.py`](../core/structure_spec.py)、[`core/config.py`](../core/config.py) 与 [`config.example.yaml`](../config.example.yaml) 将默认探洞尺度调为 `explore_radius=48`、`explore_max_visited=600`、`explore_timeout_seconds=360.0`；⑤ 新增 [`test_anchor_radius_preserves_inside_frontiers`](../tests/test_cave_probe.py#L285)、[`test_flood_graph_steps_one_block_ledge_without_meaningless_pillar`](../tests/test_cave_probe.py#L259) 与 [`test_mine_vein_picks_up_lower_floor_ore_drops`](../tests/test_cave_explore.py#L486)。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| 全仓离线 `unittest`（最后一次代码修改后复跑，`$env:BOTSLEEP_RUN_LIVE_TESTS='0'`） | **951 项运行，OK（`skipped=10`，66.041s）**；跳过的 10 项为需本地 Ninjabrain JAR 的要塞用例、需连实服的 `test_speedrun_portal.py` 及 `test_cave_matrix.py`，不计作实服通过。 |
| `main.py --config config.example.yaml --check-config` + `compileall` | 输出 `Configuration valid.`，字节码编译通过；`git -C ..\mc-agent-api status -s` 无任何改动。 |
| **深层大矿洞实服递进对照**（`SleepBot` 传送至 `[-99.5, -42, -982.5]`） | ① 初轮（`task-4397`）：131 格，3 次无意义 `pillar_up`，仅拾得 `raw_iron: +1, diamond: +2`；② 中轮（`task-4514`）：消搭高并走 173 格，但触及 `explore_radius=34` 后因越界前沿占满前 16 位而止步；③ **最终两级 4500 节点穷尽实测（`task-4570`，`explore_radius=55, timeout=300s`）**：零卡顿遍历 **`477` 格（横跨至 `X=-46, Z=-1030`）**，主动凌空搭桥跨越峡谷并搭柱攀采 `[-66, -37, -985]` / `[-67, -36, -983]` 深层铁矿与 `[-59, -43, -993..-992]` 双钻石矿（新增入包 `diamond: +2, raw_iron: +1`），至 300s 时间窗口收尾后经 `_recover_step_with_graph` 稳步走回起点 `[-99.5, -42, -982.3]`（`HP=20, Food=20`）。 |

## 历史：大型复杂矿洞 3D 连通探索、消除无意义搭高与实地捡矿验证（2026-11-16）

本轮针对实服洞穴探索“宽大厅原地打转兜圈、单格搭柱后全盘禁用快速撤退、不可达洞口空跑与连续脚本断连”问题完成排查与修复（相邻 `mc-agent-api` 未作任何修改；`starter 9 / daily 31 / competition 36` 模型目录保持不变）：① 在本机配置并接入只读/侵入调试 MCP 服务 `minecraft-debug`（`mc_list_players`、`mc_inspect_player`、`mc_watch_motion`、`mc_voxel_map` 等），通过共享 RCON 与只读会话复用零干扰采集实服玩家 XYZ/Yaw/NBT 与 3D 方块视图；② [`core/cave_probe.py`](../core/cave_probe.py) 与 [`skills/cave_explore.py`](../skills/cave_explore.py) 新增 `covered_cells` 空间覆盖过滤、`goal_hints` 主动矿脉提示、近端 `frontiers` 长路径（`path[1:16]`）优先推进及 `oy in (-2..5)` 矿脉壳层扫描；③ 修复 `pillar_undo` 非空后禁用环路剪枝、禁直接 `stand_cell(entry_cell)` 及 `explore_cave` 内部多步空退的缺陷；④ `navigate_with_ops` 扩图至 `420` 节点并在 `best_dist > 3.5` 且无有效收敛时零步快速失败；⑤ [`tests/test_cave_explore.py`](../tests/test_cave_explore.py) 新增 `test_wide_chamber_avoids_circling_and_shortcuts_retreat` 与 `test_navigate_with_ops_fails_fast_on_unconnected_pocket`。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| 全仓离线 `unittest`（`$env:BOTSLEEP_RUN_LIVE_TESTS='0'`） | **948 项运行，OK（`skipped=10`，33.187s）**；跳过的 10 项为需本地 Ninjabrain JAR 的要塞用例、需连实服的 `test_speedrun_portal.py` 及 `test_cave_matrix.py`，不计作实服通过。 |
| `main.py --config config.example.yaml --check-config` + `compileall` | 输出 `Configuration valid.`，字节码编译全部通过；`git -C ..\mc-agent-api status -s` 无任何改动。 |
| **实服对比复测**（`SleepBot`，`attach_or_login` 全程在线 + `mc_watch_motion` 采样） | 修复前首轮被困 7×10 洞角走 `91` 步（45 步前探+45 步折返）且 Cave 1 后因 `low_torch` 早退；中期修改虽突围至 `[183, 53, -674]`，但因一处 1 格 `pillar_up` 导致 `pillar_undo` 禁用直跳，仍消耗 `106` 步（含 `43` 步逐格撤退与 `8` 步误入 Cave 1 朝向 Cave 2）；最终实测（`task-4332`）全程仅 **`46` 步、覆盖 `43` 个唯一坐标点**（仅 3 处边界格路过 2 次、无局部转圈），**`STEP 44` 以单次 `stand_cell` 直接从洞深返回洞口 `[186, 52, -672]`（2.4s）**，不可达的 Cave 2 `[180, 39, -666]` 经 `420` 节点图判定后 **0 冗余步直接跳过**，`SleepBot` 保持在线（`HP=20`）。 |

## 洞穴稳健探索、防退游戏/防轻伤弃洞与在线会话复用验证（2026-11-16）

本轮针对实服探洞暴露的 `missing_torch` 拒入、每转角密集插火把、1~2 点擦伤触发 `damage_taken`/`hurt` 退洞以及多脚本退出断连问题完成改造（相邻 `mc-agent-api` 未作任何修改；`starter 9 / daily 31 / competition 36` 模型目录保持不变）：① [`core/structure_spec.py`](../core/structure_spec.py)、[`core/config.py`](../core/config.py) 与 [`config.example.yaml`](../config.example.yaml) 将 `CAVE_LIMITS` 调整为 `torch_interval=24`、`min_enter_torches=0`、`explore_radius=16`、`explore_max_visited=120`、`explore_timeout_seconds=180.0`，并增加 `mc_api.keep_session_on_exit` / `session_file`；② [`core/client.py`](../core/client.py) 与 [`main.py`](../main.py) 为 `move_to`/`place_block`/`use_item` 增加可选 `stop_on`，新增 `MinecraftAgentClient.attach_or_login`/`save_session`/`load_session` 及 `--keep-connected`（`competition` 模式默认保持在线不调 `logout`）；③ [`skills/action_workflow.py`](../skills/action_workflow.py) 为 `WorkflowGuard` 增加 `damage_tolerance`/`critical_health=6.0` 与 `stop_on=["death"]` 转发，并新增 `scaffold_drop` 与 `escape_to_surface`；④ [`core/cave_probe.py`](../core/cave_probe.py) 与 [`skills/cave_explore.py`](../skills/cave_explore.py) / [`skills/mining.py`](../skills/mining.py) 实现无火把可入洞、自动合火把与洞内进食回血、`lateral_air_clearance` 洞穴中线优先、搭石过岩浆 `bridge`、垫方块安全降落差 `scaffold_drop`、单步卡死循环内重规划、撤退受阻回地面 `escape_to_surface`，并将 `allow_damage=True` 设为默认。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| 专项离线回归（`test_cave_probe`、`test_cave_explore`、`test_mining_workflow`、`test_client`、`test_runtime`） | **76 项全部通过（14.856s）**，覆盖 0 火把入洞、显式 `min_enter_torches=1` 门禁、24 格火把稀疏间隔、2 点擦伤不弃洞、洞穴中线评分高于贴墙格、`bridge` 搭石跨岩浆、`scaffold_drop` 分段垫脚下落、撤退阻塞时 `escape_to_surface` 搭柱脱困回地面、`attach_or_login` 会话复用与 `--keep-connected` 退出不登出（均为离线/内存模拟）。 |
| 全仓离线 `unittest`（`$env:BOTSLEEP_RUN_LIVE_TESTS='0'`） | **946 项运行，OK（`skipped=10`，37.474s）**；跳过的 10 项为需本地 Ninjabrain JAR 的要塞用例（5 项）、需连实服的 `test_speedrun_portal.py`（3 项）及需连实服 RCON 的 `test_cave_matrix.py`（2 项），未计作实服通过。 |
| `main.py --config config.example.yaml --check-config` + `compileall` | 输出 `Configuration valid.`，`UNIT=0 CHECK=0 COMPILE=0`。 |

## 原地柱搭高、洪水子图规划与优先探洞验收（2026-11-16）

本轮在不修改 `mc-agent-api` 的前提下实现并验证了：① [`skills/action_workflow.py`](../skills/action_workflow.py) 的原地柱上升 `pillar_up`（`WorkflowGuard.set_jump` + `place_block` + `finally` 复位）与安全下降 `pillar_down`；② [`core/cave_probe.py`](../core/cave_probe.py) 的安全可达子图洪水规划 `flood_safe_cave_graph`（含步行、`opens_to_air` 约束微掘与受保护原地柱，同时以 `feet_corner_dist <= 3.95` 对齐 `mc-agent-api` `goNear(3.5)` 挖矿距离）；③ [`skills/cave_explore.py`](../skills/cave_explore.py) 的大洞安全子图（`allow_tall_cave_subgraph`）、岩石顶板洞口检索 `find_cave_openings` 与陡坡 `navigate_with_ops`、`discover_and_explore_caves`；④ [`skills/mining.py`](../skills/mining.py) `MineShaftSkill.run_with_journal(..., prefer_caves_first=True)`。模型工具目录（`starter 9 / daily 31 / competition 36`）保持不变。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| 专项离线回归（`test_cave_probe`、`test_cave_explore`、`test_mining_workflow`、`test_container_skill`、`test_mining_targets`） | **27 项全部通过（10.888s）**，覆盖高顶含岩浆隔断洞穴 `tall_subgraph` 原地柱上下、`opens_to_air` 穿墙微掘与防掘断柱基座保护、陡坡 `stalled` 恢复与全图洪水路径。 |
| **实服 RCON 原地柱稳定性专项**（5 轮 × 4 格连续搭高，共 20 步） | **`ok=20/20`、`first_try=18/20`、`fail=0`、平均耗时 `847ms`/格**，验证了仅靠 `posture(jump=True)` + `place_block` 无需改动 `mc-agent-api` 即可稳定搭柱。 |
| **实服 RCON 洞内微掘与原地柱采高矿专项**（`doMobSpawning false`） | ① 微掘穿石墙阻隔并避开岩浆角安全采回 `raw_iron=2`（`47.02s`，`HEALTH=20`）；② 黑曜石高台强制触发 `pillar_up` 上攀采下 `[255, 74..75, -97]` 共 `raw_iron=2` 后由 `pillar_down` 安全拆柱回到起点（`HEALTH=20`）。修复了高处矿块因 `feet_corner_dist > 4.0` 引发 `mc-agent-api` `dig` 调用 `goNear` 报 `path_not_found` 的边界问题。 |
| **实服自然地形优先探洞检验与边界说明** | 修复前 `[150.5, 120, -244.5]` 与 `[264.5, 96, -232.5]` 分别耗时 `119.4s`（3 处）与 `112.33s`（2 处）均零伤通过陡坡地形但 `HARVESTED={}`（原因：无露头铁矿且树冠叶片误判为洞口）；增加 `rock_roof_blocks` 岩石顶板校验后复测 `[150.5, 120, -244.5]` 精确过滤树冠（`0` 误报），`[264.5, 96, -232.5]` 精确锁定唯一石顶山洞 `[240, 88, -245]`（`160` 格）。注：自然随机地表若本身无露头铁矿，仍需 `prefer_caves_first=True` 自动衔接下挖并切入地下大洞。 |

## 容器存取熔炼与随机地形 30 铁目标验证（2026-11-16）

本轮新增独立容器与熔炉技能 [`skills/container.py`](../skills/container.py)（`ContainerSkill`：双箱/木桶 `inspect`/`deposit`/`withdraw` 与自动配燃料、清既有产物及支持 `>16` 组轮询的 `smelt`），将其接入 `MineShaftSkill.rest_at_workstation`，并将 `MineShaftSkill` / `CaveExploreSkill` 扩展为支持 `target_y=16` 安全深梯、`mining_mode="fishbone"`（3 格间隔左右肋道）、保留转向角点的 `deep_trail`、时间片预留退回 `timeout_checkpoint`、物理背包增量对账与矿滴靠近拾取（移除按块虚增 `max(gained, 1)`）。相邻 `mc-agent-api` 仓库保持完全未修改；`starter 9 / daily 31 / competition 36` 模型工具目录保持不变。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| 专项离线回归（`test_cave_probe`、`test_mining_targets`、`test_cave_explore`、`test_mining_workflow`、`test_container_skill`） | **24 项全部通过（5.011s）**，覆盖双箱批量存取、满箱 `partial`、30 粗铁配煤轮询熔炼、鱼骨 30 铁配额停机、不安全洞穴封口与检查点恢复（模拟环境）。 |
| 全仓离线 `unittest`（`$env:BOTSLEEP_RUN_LIVE_TESTS='0'`） | **939 项运行，OK（`skipped=10`，28.119s）**；跳过的 10 项为需本地 Ninjabrain JAR 的要塞用例（5 项）、需连实服的 `test_speedrun_portal.py`（3 项）及需连实服 RCON 的 `test_cave_matrix.py`（2 项），不计作实服通过。 |
| `main.py --config config.example.yaml --check-config` + `compileall` | 输出 `Configuration valid.`，`UNIT=0 CHECK=0 COMPILE=0`。 |
| **实服容器与熔炉专项**（双箱 + 熔炉直接验收，`doMobSpawning false`） | `DEPOSIT`（`cobblestone: 32, dirt: 12`）、`INSPECT_DOUBLE_CHEST`（54 槽全量读回）、`WITHDRAW`（`dirt: 5`）、`SMELT`（`3 raw_iron -> 3 iron_ingot`，实际背包确认 `iron_ingot=3`）全部通过。注：工作站大箱子上方必须留空，否则 `mc-agent-api` 返回 `no_window`。 |
| **实服随机未改地形 30 铁目标终验**（`[338.5, 69, -246.5]`，3 轮检查点循环，共 `1080.95s`） | **完成（`STOP=target_reached`）**：下探至 `Y=16` 鱼骨主干推进 `TRUNK=52`（`[346, 16, -145]`），安全处理/封口 `CAVES=7`；第 1 轮存入大箱 `cobblestone x287` 等杂物，第 2 轮存入 `cobblestone x269` 并起炉熔炼 `raw_iron x16`，第 3 轮在 `242.72s` 内凑齐剩余 `raw_iron x14` 并触发 `target_reached`。最终 `FINAL_SMELT` 炼完并验证 `FINAL_INV iron_ingot=30`、`FINAL_CHEST cobblestone=556`（含 `dirt=16, andesite=101, diorite=66, granite=7, gravel=8`），零死亡。 |
| 实服过程发现与边界说明（诚实记录） | 早期实测曾暴露 4 类问题：① `[140.5, 121, -249.5]` 高山陡坡在 `move_to_start` 阶段出现 `stalled`（`10.12s` 中止）；② `[281.5, 92, -175.5]` 仅单轮 `540.66s` 未到 30 铁（`raw_iron=4`）；③ `[309.5, 70, -248.5]` 因旧版 `max(gained, 1)` 虚增在实收 `19 iron_ingot` 时提前停机（已修复并重跑验证 30 铁）；④ `[56.5, 90, -154.5]` 多转向深梯因 `deep_trail[::6]` 丢角点在第 2 轮 `stalled`（已改为保留转向角点并按路径长度动态预留退回时间）。 |

## Plan v2：矿道遇洞穴处理、独立洞穴探索、目标挖矿与检查点续探（2026-11-16）

本轮新增并验证了纯逻辑探测/分类/可达扫矿 [`core/cave_probe.py`](../core/cave_probe.py)、目标与回家阈值解析 [`core/mining_targets.py`](../core/mining_targets.py)、独立洞穴探索技能 [`skills/cave_explore.py`](../skills/cave_explore.py) 及其与 [`skills/mining.py`](../skills/mining.py)（`MineShaftSkill`）的联动，同步补充了 `SurvivalJournal.mining_progress` 检查点、`core/config.py` / `config.example.yaml` 的 `mining` 配置段，以及 `DailySurvivalGoals.mining_rest_action`。相邻 `mc-agent-api` 仓库保持未修改；`starter 9 / daily 31 / competition 36` 模型工具目录保持不变。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| 离线专项回归（`test_cave_probe`、`test_mining_targets`、`test_cave_explore`、`test_mining_workflow`、`test_config`） | **51 项全部通过**（`0` 失败、`0` 错误、`0` 跳过），覆盖水洞/高洞/深落差/岩浆/刷怪笼封口换向与 3 次放弃、矮宽洞不按空气格数误拒、废弃矿井与蜘蛛网/矿车规则、8 面扫矿/配额/镐级/危险邻格、`collect=False`、`return_home_requested` 与 `pending` 禁止恢复；以上均基于内存模拟世界。 |
| 离线全仓 `unittest`（`$env:BOTSLEEP_RUN_LIVE_TESTS='0'`） | **934 项运行，OK（`skipped=9`）**，耗时约 `25s`；跳过的 9 项为未安装本地 Ninjabrain JAR 的要塞用例（5 项）、需连实服的 `test_speedrun_portal.py`（3 项）及需连实服 RCON 的 `test_cave_matrix.py`（1 项 / 后续扩展为 2 项），不计作实服通过。 |
| `main.py --config config.example.yaml --check-config` + `compileall` | 输出 `Configuration valid.`，`UNIT=0 CHECK=0 COMPILE=0`。 |
| **实服 RCON 矩阵验收**（`$env:BOTSLEEP_RUN_LIVE_TESTS='1'`，`tests/test_cave_matrix.py`，`doMobSpawning false`） | **2/2 全部通过（108.107s）**：① `[250, 72, -120]` 主干撞水洞（`water_cave`）与侧支撞高洞（`tall_cave`）后均用实心 `cobblestone` 成功封口、原路退回并自动切换方向完成安全分支；② `[210, 72, -120]` 完成 5 层楼梯 + 4×3×2 平台 + 工作站（床与右键重生绑定、工作台+顶部熔炉、2 宽大箱子合并、墙火把），进入废弃矿井后成功联动 `CaveExploreSkill` 插火把、挖 `iron_ore`（`collect=False`）、跳过 `copper_ore` 记为 `seen`，并在地下工作站执行存箱与起炉熔炼 `rest_at_workstation`。修复了实服中暴露的两处真实问题：整数格 `stand_at` 目标偏移 `+0.5` 引发的微距偏差（新增 `stand_cell`）及回归分支对 `CaveExploreSkill` 所插 `wall_torch` 的误报（改用 `is_passable` 并缓存已探洞穴体素避免重复入洞）。 |

## 历史：`develop-tianya` 速通传送门架构与自然岩浆池实测（2026-11-16）

本轮新增并验证了通用结构规约 [`core/structure_spec.py`](../core/structure_spec.py)、动作工作流 [`skills/action_workflow.py`](../skills/action_workflow.py) 以及速通地狱门技能 [`skills/portal_speedrun.py`](../skills/portal_speedrun.py)（含 `keep-alive` 只读重连保护 [`core/client.py`](../core/client.py)）。相邻 `mc-agent-api` 仓库保持完全未修改。

| 检查 / 实测轮次 | 结果与说明 |
| --- | --- |
| 离线专项回归（`test_structure_spec`、`test_action_workflow`、`test_portal_workflow`、`test_client`、`test_contracts`） | **96 项全部通过**（`0` 失败、`0` 错误、`0` 跳过）。 |
| 离线全仓 `unittest`（`PYTHONPATH=.;tests`，排除需连活服的 `test_speedrun_portal.py`） | **901 项运行，OK（skipped=5）**；跳过的 5 项均为缺少本地 Ninjabrain JAR 的要塞引擎/校准用例。 |
| `main.py --config config.example.yaml --check-config` + `compileall` | 输出 `Configuration valid.`，字节码编译无误。 |
| R7 实服自然岩浆池基准（Batch 7 代码，12 处全新未改地形：6 地表 + 6 地下） | **9/12（75.0%）**，其中地下洞穴 `6/6（100%）`、地表 `3/6`；3 处失败均为底座倾倒前缘 `damage_taken`。 |
| R8 实服自然岩浆池基准（Batch 8 修复 `GoalNearXZ` 前缘微调，12 处全新未改地形：6 地表 + 6 地下） | **9/12（75.0%）**，地表 `4/6`、地下 `5/6`，**全部 12 处 `health=20`、`on_fire=False`**（零受伤、零着火）；3 处失败为 `dig_unconfirmed`、`unreachable_stance` 与 `radius=24` 截断导致 `no_portal_site_found`。 |
| R9 实服自然岩浆池基准（Batch 9 扩展安全清障与 `radius>=32` 探测，12 处全新未改地形：6 地表 + 6 地下） | **4/12（33.3%）**；清障与选址探测正常，但 6/8 失败源于侧壁倾倒保护阈值 `max_back=0.46` 过严误拦 `0.50` / `0.70` 常规贴边站位。收尾阶段已将侧壁安全阈值修正为基于射线几何截断的 `0.52`（离线单测已重跑验证；尚未重新全量跑 12 池活服）。 |

> 说明：R7–R9 尚未达到此前设定的 ≥ 90%（≥ 11/12）自然岩浆池绝对通过率目标，后续如继续冲刺 >90%，重点需针对斜坡落差站位（`dy > 1` 接近/搭坡）及路径寻路贴边停在 `0.70` 时的垫脚/侧向绕行补强。

## 历史：`develop-tianya` 多人协作结构与稳定性验证（2026-11-15）

本轮只跑离线测试与静态契约校验，没有连接 Minecraft、mc-agent-api、RCON 或模型。实服速通用例与缺少本地 JAR 的 Ninjabrain 引擎用例按规范显式跳过（`skipped=8`），不伪装为实服或引擎完整验收。

| 检查 | 本轮结果 |
| --- | --- |
| `.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_contracts.py" -v` | **8 项通过**，覆盖全仓 AST 语法与 `with sqlite3.connect` 句柄泄漏静态拦截、starter/daily/competition 工具目录与技能 `ARGUMENTS` / `SKILL_REGISTRY` 一致性、`core/domain.py` 纯函数，以及 `SurvivalJournal.instance_lock()` 跨进程互斥与 `main.py` 重复启动拦截。 |
| `.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_botctl.py" -v` | **3 项通过**，覆盖 `scripts/botctl.py` 离线只读检查（空闲可替换、实例锁/`pending` 阻塞、`ModelRequestGate` `uncertain` 阻塞与确认后放行）。 |
| `.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p "test_survival_memory.py" -v` | **30 项通过（0 错误，0 `ResourceWarning`）**；修复 `test_corrupt_and_future_schema_databases_are_preserved` 中裸 `with sqlite3.connect(...)` 未关闭文件句柄导致的 Windows `WinError 32`。 |
| `.\.venv\Scripts\python.exe -X utf8 -B main.py --config config.example.yaml --check-config` | 输出 `Configuration valid.`，退出码 `0`。 |
| `.\.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -q` | **853 项运行，0 失败，0 错误，8 项显式跳过（`OK (skipped=8)`）**；其中 5 项为未安装本地 Ninjabrain JAR 的引擎/校准跳过，3 项为离线环境下 `test_speedrun_portal.py` 未连接实服的显式跳过。 |

## 历史：拉取后复核（3801be9）

环境：Windows、Python 3.11.3、requests 2.34.2、PyYAML 6.0.3、Temurin OpenJDK/javac 25.0.2。已有虚拟环境，本轮未安装依赖。只使用离线测试和本机临时假 HTTP 服务，没有连接 Minecraft、实际 mc-agent-api、RCON 或模型，也未修改相邻 API 仓库。

| 检查 | 本轮结果 |
| --- | --- |
| `$env:BOTSLEEP_RUN_LIVE_TESTS='0'; python -X utf8 -B -m unittest discover -s tests -q` | **845 项，39.857 秒：841 项通过、3 项跳过、1 项 ERROR**；`FAILED (errors=1, skipped=3)`，不能报告全绿。 |
| `python -B -m unittest discover -s tests -p test_live_test_isolation.py -v`（门禁关闭） | 3 项通过；子进程禁止网络连接与 mcrcon/core.rcon 导入，验证门户 unittest 跳过、独立合成/随机地形脚本拒绝执行。 |
| `python -X utf8 -B main.py --config config.example.yaml --check-config` | `Configuration valid.`；只核对示例配置，不读取私有服务状态。 |
| 工具、目录与配方读取 | starter/daily/competition 为 9/31/36 个模型工具；125 项赛事目录、7 个行动计划；RecipeBook 加载 886 个产物，并读取木镐/木板配方及网格分类。不代表全部配方执行正确。 |
| Python AST / 文档链接 | 101 个受版本控制或新增 Python 文件语法通过；69 个文档相对链接有效，未发现合并冲突标记或本机绝对路径，`git diff --check` 通过。 |

唯一 ERROR 仍是 `test_survival_memory.SurvivalMemoryTests.test_corrupt_and_future_schema_databases_are_preserved` 清理临时 `memory.sqlite3` 时的 WinError32，后续 WinError267。首轮 842 项（39.024 秒）与最终复跑均出现同类错误；不是业务断言失败，但根因与修复仍未完成。本轮不删除原数据、不忽略清理异常，也不改记忆模块掩盖错误。

3 项跳过全部来自 `tests/test_speedrun_portal.py`，不是缺少 Ninjabrain JAR；5 项 Ninjabrain 引擎/离线 CLI 用例实际执行且未跳过。本轮没有新的实服门户验收。`test_crafting.py` 和 `test_random_lava_matrix.py` 是直接运行的脚本，默认 discover 只导入，不包含 unittest 用例；不能把被发现或导入当作这些能力测试通过。旧代码中的“已验证”、零伤害、20 轮/90% 成功率描述是目标或历史主张，没有本轮新日志支撑。

最新源码增加的实服测试若直接 discover 原会登录并用 RCON 改世界。本轮加 `BOTSLEEP_RUN_LIVE_TESTS=1` 门禁，RCON 依赖延后导入；显式执行还需 `MC_AGENT_API`、`MC_AGENT_PASSWORD`、`MC_RCON_PASSWORD`，可选 `MC_RCON_HOST/MC_RCON_PORT`。固定坐标命令针对 SleepBot，其他 `MC_AGENT_USER` 被拒绝。只在已授权的专用测试世界启用；运行命令及限制见 [DEBUG_GUIDE.md](DEBUG_GUIDE.md)。

模型主链仍没有绑定 `PROCEDURAL_TOOL_DEFINITIONS`，独立传送门/整树伐木/配方合成/雷达不是新增计分行动计划。`PortalTelemetryRecorder` 主动读取与 JSON 导出也未接入 EventMonitor，见 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [MONITORING.md](MONITORING.md)。

## 历史：赛事任务工作区验证（2026-11-15，c21d623 → 783e152）

该轮只跑离线测试，没有连接 Minecraft、mc-agent-api、RCON 或模型。当时的工作区改动后经 `783e152` 提交并已合并；下表只作历史基线，不代表当前结果。

| 检查 | 本轮结果 |
| --- | --- |
| `python -B -m unittest discover -s tests -p 'test_competition_*.py' -q` | **67 项通过**，覆盖规则目录、服务器计分、缓存过期、任务生命周期、终态恢复、赛事预算、配置、客户端与赛事面板。 |
| `python -B -m unittest discover -s tests -p test_monitor.py -q` | **23 项通过**，覆盖事件监控与只读 HTTP。 |
| `python -X utf8 -B -m unittest discover -s tests -q` | **839 项，25.143 秒，1 项 ERROR**；唯一错误为 SQLite 临时文件清理 WinError32，后续 WinError267。不是业务断言失败，但不能报告全绿。 |

赛事边界：45 项明确计分目标中只有 7 项有显式行动计划，其余 scored 项虽可由服务器 advancement 评估和计分，但当前标记 unsupported、不可选择。54 项 conditional 默认不计分，只有显式 eligibility override 才启用；规则来源是 draft v2。score 只纳入服务器确认，不推算 normalized。结束检测评估 120 个非根目标；未知值可被记录为 unknown，但不能得分。

## 历史独立复核（2026-11-14）

旧虚拟环境缺少 PyYAML；安装 `requirements.txt` 后为 PyYAML 6.0.3，再进行全量验证。缺依赖时的首次运行含导入错误，不作为有效全量结果。

| 检查 | 本轮结果 |
| --- | --- |
| `python -X utf8 -B -m unittest discover -s tests -q` | **772 项，17.131 秒，0 跳过，`FAILED (errors=1)`；未全绿。** |
| `python -X utf8 -B -m unittest discover -s tests -p test_survival_memory.py -k corrupt_and_future_schema -v` | 1 项，0.049 秒，ERROR；定向复跑再次出现同类清理错误。 |
| `python -X utf8 -B main.py --config config.example.yaml --check-config` | 明确输出 `Configuration valid.`，通过；仅配置检查，不连接游戏。 |
| `python -m integrations.ninjabrain calculate --input data/examples/stronghold_throws.json` | 合成示例 CLI 成功，首选区块 `[128,0]`；不是实际世界定位或投眼验收。 |
| 工具目录与任务图实际导入核对 | starter 9项工具、daily 31项工具、daily 13项任务；`calculate_stronghold` 不在两工具目录中。 |
| 固定 Ninjabrain JAR 校验 | 通过。 |
| Python AST 语法检查 | 83个 Python 文件通过 `ast.parse`；不替代运行或行为验收。 |
| 相对 Markdown 链接核对 | 当前60个相对链接无损；不代表忽略目录中的私有证据已随 Git 分发。 |

全套唯一错误为 `test_survival_memory.SurvivalMemoryTests.test_corrupt_and_future_schema_databases_are_preserved`：Windows `tempfile` 清理临时目录中的 `memory.sqlite3` 时报告文件占用（WinError32），随后出现 WinError267。定向复跑再次出现同类错误。这不是业务断言失败，但仍使测试结果为 ERROR；尚未证实占用来源和根因，不能宣称已解决。更早一次单例尝试遇到 Python 标准库 TypeError，未取得有效隔离结果，不作为根因证据。

本轮仅更新文档，不修代码、不追加测试；未运行游戏、API、模型或真实投眼。代码核对确认配置驱动 sleep/starter/daily、单动作执行、独立单模型 worker、原生工具反馈、starter 9项/daily 31项工具及 SQLite 记忆已接入。游戏动作 `pending` 需 `--resume` 后重新观察，模型跨进程门禁的未知请求则需确认远端空闲后显式 `--acknowledge-model-idle`，两者独立。要塞仍仅独立 Skill/schema/CLI；完整赛事接口、Pi/OMP 式语义 compact、通用 reflex 任务恢复和水面导航等提案见 [HARNESS_INTERFACES.md](HARNESS_INTERFACES.md)，不能由日常运行时已接通推断全部落地。

以下历史报告中的 `.test-runtime/`、`.survival-test-runtime/`、`logs/` 及引擎缓存均受 Git 忽略，不随拉取分发。缺少原始证据时需获取脱敏副本或重新验证，不能仅凭路径认定接收端已有文件。

## 历史：2026-11-14 dev-ink 与 main 合并验证

在隔离工作区合并 `dev-ink 8ae68e9` 与远端 `main 65478da`，保留日常生存实现和上游要塞计算、投眼采样、接口设计文档。运行中的原工作区、API与游戏服务未重启或替换。下方15:40–16:35的实服记录属于原 `dev-ink` 运行，不代表合并后重新做过实服验收。

- 历史合并记录 `python -B -m unittest discover -s tests -q`：**772项通过，17.137秒，0跳过**；不替代其他环境的 ERROR 记录或本文首节的新结果。包含740项日常/原有回归、31项要塞模块测试及1项合并工具目录回归。
- Ninjabrain 1.5.2 官方JAR按固定SHA-256校验；JDK24.0.2下全部5项引擎/离线CLI测试运行通过。缓存JAR与编译产物受Git忽略，不纳入提交。
- `python -m compileall -q main.py agent core skills debug integrations`、示例配置 `--check-config`、监控JavaScript语法检查通过。
- 保留新版统一HTTP及未知动作处理；兼容 `MinecraftAPIError` 旧异常名称，投眼采样通过 `inventory()["result"]` 读取经验证的背包。7项新增回归覆盖实际客户端与模拟HTTP的返回结构、offhand、业务失败、非法库存、超时不重投及异常兼容。
- `calculate_stronghold` 的原schema保存为独立 `STRONGHOLD_TOOL_DEFINITION`；独立Python技能和CLI保留。生存模型目录仍为starter9项、daily31项，不广告尚未绑定调度器的动作。

历史合并验证没有登录采样、投眼、修改存档、调用模型探测或恢复死亡预算。18:32历史只读监控记录为HTTP200，原进程已因 `death_limit` 安全暂停；这不是当前在线状态声明。

## 历史：2026-11-14 日常生存实现与验证

### 修复后的加载与回归（16:26，Asia/Shanghai）

740项全套离线测试通过（15.126秒），较15:40版本增加28项：对角清叶3项、局部遮挡规划2项、失败状态恢复6项、紧急撤退观察15项、部分观察模型反馈2项。监控JavaScript语法检查通过，完整测试包含本机监控HTTP检查。

16:26:21在跨进程模型门禁确认 `idle`、游戏动作空闲后仅替换BotSleep进程，stdout `.test-runtime/daily-20261114T082621Z.stdout.log`。当时API/Java源码和进程未改；没有清除模型未知台账。监控 `http://127.0.0.1:18930/` 已恢复，初始生命/饥饿17、位置 `[1.7,122,0.5]`，夜间本地安全阶段。16:27实际触发短安全观察与近敌撤退检查；未发现合规终点时返回 `unsafe` 后等待新观察，没有把零位移算成脱困。

前一会话完整快照归档于 `.test-runtime/`，11步反馈链另见下节报告。新进程自己的清叶与采集证据须单独登记，不能把前一会话成功算作新补丁已通过实服验收。

16:35继续核验：新会话已80步、未暂停、监控HTTP200、stderr空；生命/饥饿17，仍在 `[1.7,122,0.5]`。白天 `time_of_day=8250`，但 `thundering=true` 且最近苦力怕6.5格，因此安全逻辑仍阻止生产。前两次模型请求分别于08:28:36/08:29:59 UTC发出，近敌抢占后等待返回并丢弃，最终 `llm_cancelled`；未并发，也没有可用 `LLM_RESPONSE`，本会话实际模型名仍未确认。08:32:49 UTC前的固定观察窗口内33次等待、27次撤退，撤退全为 `unsafe/executed=false`；新对角清叶、采集、制作均未实际执行。不能将其称为新补丁已经脱困，也不能把本地取消称为模型服务失败。限定窗口报告位于 `.test-runtime/`；当时运行快照 `.test-runtime/live-daily-verification-20261114.json`。当时机器人、API、游戏和实时监控继续运行，未改变天气或地图；此处不推断当前状态。

### 清叶、采集、制作与失败反馈实测（15:50–16:03，Asia/Shanghai）

当时官方 API 为 `1cfecc3` / 1.3.7。同一会话的 stdout 为 `.test-runtime/daily-20261114T074005Z.stdout.log`；以下均来自同一自然生存世界，没有给物、传送、修改时间或布置测试场。

| 北京时间 | 实际结果 |
| --- | --- |
| 15:50:46 | `clear_foliage` 清除 `[6,115,-5]` 橡树叶，目标复核为空气；原位操作、`collect=false`。 |
| 15:51:23 | `gather_block` 移除 `[6,115,-4]` 橡木，库存原木 `+1`。 |
| 15:51:53 / 15:52:21 | 木板 `+4`、木棍 `+4`，均按真实库存变化确认。 |
| 15:52:46 | 制作工作台返回后端 `missing_ingredients`；当时仅剩木板2个。API 的示例配方木种不代表必须使用该木种。 |
| 15:53:26 / 15:54:11 | 首次移动无位移，返回 `move_unverified`；模型随后改去已有工作台，实际从 `[7.7,115,-4.3]` 到 `[7.5,111.9,-3.5]`，之后观察 Y=111。 |
| 15:54:45 / 15:55:14 | 补采原木 `+1`，再次制板实际 `+4`。 |
| 15:56:09 | 制成木镐，库存 `wooden_pickaxe +1`。 |
| 16:03:19–16:03:26 | 夜间撤退只完成第一段树叶中继；两次完整观察因位置或生命变化失效，未出现第二段移动。游戏服务日志明确记录 `SleepTestBot was slain by Zombie`，随后重生。 |

完整响应自报 `qwen3.6-35b-a3b`。步骤84–94的11个动作均核对了模型调用、实际结果、下一请求中相同 `tool_call_id` 的反馈及后续响应，11/11配对正确。对应脱敏报告位于 `.test-runtime/`，含原日志行号。缺料后的下一请求15:52:53带上真实失败，15:53:23模型改选移动；移动失败后再次收到配对反馈并改换落点、补采，最终借助已有工作台制出木镐。当时不能计作成功自制工作台。

清叶、采木、制作均有后验，不再仅是模型提议；初始树冠已有实际下降，但一次清叶和下降不能证明所有树冠可安全退出。16:03:32重生后当时库存为空，不能把死亡前木镐继续记为持有。夜间死亡也说明长期生存尚未通过。`dig_unconfirmed` 本次未自然触发，仍只能引用离线覆盖。

重生后发现并修复：可见对角叶片被四邻限制拒绝；近处有遮挡的原木被远处未知可见性的资源挤出模型快照；清障后旧寻路失败未及时失效；撤退续段被完整生产观察拖延。清叶改为八邻身体高度，仍禁止自身整列和支撑，不盲挖或追掉落物；短撤退观察仍需新鲜状态和实体，技能逐段重新验证地形。

### 历史代码加载点（15:40，Asia/Shanghai）

15:40加载版本的整合测试 **712 项全部通过（12.008 秒）**，包含草取种和局部清叶。15:40:05 在跨进程模型锁确认 `idle`、游戏动作空闲后替换旧机器人，stdout `.test-runtime/daily-20261114T074005Z.stdout.log`。当时API/Java未改源码或重启。新进程已加载 31 工具版本，夜间本地安全处理运行，HP/food20，位置 `[7.7,115,-4.3]`；未清除任何模型未知台账或游戏历史。

此前同一天的新 API 会话中，07:29:03 UTC 的请求带上夜间拒绝的原生工具反馈；模型改选远树，07:31:48 后端真实返回 `stalled`，随后模型改为移动到工作台，07:32:41 仍 `stalled`。第二会话于07:33:50明确读到近木 `[6,115,-4]` 被 `[6,115,-5]` 遮挡，因而补齐局部清叶与可见性变化后重新规划。不能将“改选行动”记成采集或制作成功，也不能把零移动的安全拒绝记成脱困成功。

截至15:40的历史检查点，这些先前会话完整响应均自报 `qwen3.6-35b-a3b`；新进程当时刚处于夜间安全阶段，尚未有自己的模型响应。后续同会话清叶、采集、制作及死亡见本文顶部实测表，完整日常链仍待验收。报告保留为 `.test-runtime/live-daily-first-session-20261114.json`、`live-daily-second-session-20261114.json` 和最新 `live-daily-verification-20261114.json`。

### 新日常会话的真实检查点（15:23，Asia/Shanghai）

15:17:06 已结束只读维护进程并启动新 daily 机器人；当时官方 API、Java 未重启或修改。监控 `http://127.0.0.1:18930/` 已加载日常任务板。实际配置采用 `default`、180 秒模型超时、1000 步/4 小时预算；这只是有界运行，不是长期生存完成。

15:18:47 首次完整模型响应耗时 94.906 秒，`response.model=qwen3.6-35b-a3b`，提议采集 `[6,115,-4]` 的橡木。15:18:48 重新观察已到黄昏，返回 `safety_changed / night_requires_verified_sleep_or_shelter_route / executed=false`，生成配对工具反馈；随后仅本地等待。15:21–15:22 近敌触发多次撤退检查，有限搜索没有发现安全实地终点，返回 `unsafe / executed=false` 并冷却重新观察；没有移动请求挂起，也不能据有限样本声称整棵树物理上无路。15:23 生命/饥饿仍 20，位置 `[7.5,115,-4.5]`。

此检查点只有实际模型响应、新鲜状态拒绝旧动作和本地避险检查证据，尚无本会话采集、制作或完整日常生产链成功。未给物、传送、改时间或布置靶场。私有现场报告为 `.test-runtime/live-daily-verification-20261114.json`，stdout 为 `.test-runtime/daily-20261114T071706Z.stdout.log`；后续实测须按此会话追加。

### 离线验收范围

日常模式说明见 [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md)。新增任务图、当前设施观察、安全策略、生产技能、逐块住所建造、长期记忆与单模型门禁；本段表示离线实现和回归范围，不表示普通世界已完成长期日常生存。

**历史日常整合命令：`python -B -m unittest discover -s tests -q`，740 项全部通过，15.126 秒。** 示例与实际私密配置均通过 `--check-config`；监控 JavaScript 通过 Node 语法解析，新增日常任务板/模型槽状态通过本机 HTTP 测试，未做浏览器视觉验收。未忽略源码与文档扫描未发现实际配置凭据；官方 API `1cfecc3b92210b1fe15a0d315850e3446e556f6a` 跟踪文件及参考项目保持未修改。

新增端到端离线证据：实际 collector、技能、harness、原生模型协议与本机假 HTTP 联通；12 项日常 runtime 覆盖熔炼装填→当前槽位→产物取出、未知转移不再观察/重放、开门→重新观察→入屋→关门、前侧精确站位→侧边两格床→实际库存减少。另 3 项低血测试从真实 collector 取得完整近邻证据，稳定低血可收获直接可食作物，继续掉血或近敌则抢占；2 项模型取消测试确认排队取消不发送请求，已发出的请求需排空后才能继续。

以下模块在当时的实现验证中分别实际执行并通过，暂不与历史测试数相加作为最终全套总数：

| 命令（仓库根目录） | 结果 | 主要证据边界 |
| --- | --- | --- |
| `python -B -m unittest discover -s tests -p test_daily_observation.py -q` | 46 项通过 | 窗口最多两个且近距落地串行读；未知槽不当空；读取不确定立即结束；住所门路线；低血食物最多14格邻域；草独立采样最多48额外读；相邻叶体最多32额外读。 |
| `python -B -m unittest discover -s tests -p test_shelter.py -q` | 21 项通过 | 假世界逐次补完 55 格；每次仅一格与一件消耗；门上下半格/朝向/闭合；保持出口、身体净空及读取预算。 |
| `python -B -m unittest discover -s tests -p test_survival_memory.py -q` | 30 项通过 | API/账号/世界隔离、同名多地点、TTL、死亡/维度失效、未知观察、事务、容量、总 JSON 字节预算和脱敏。 |
| `python -B -m unittest discover -s tests -p test_model_gate.py -q` | 7 项通过 | 线程/本机进程互斥、超时或进程中断后的持久未知状态、损坏台账拒绝、明确确认空闲前不重发。 |

观察与住所的源码/测试文件通过 AST 和尾随空白检查，`git diff --check` 通过。独立模块数是上述全套总数的子集，不应再次累加；独立模块通过不代替整条生产链的游戏验证。

当时还新增 `tests/test_daily_survival.py`、`tests/test_daily_skills.py`、`tests/test_daily_policy.py`、`tests/test_daily_runtime.py`，覆盖任务依赖、实际增减验算、本地安全与模型反馈整合。回归应检查：熔炼开始不等于产出、未知/部分转移保留 pending、模型返回后当前窗口已变化时不执行旧决策、开门后重新观察再进屋再关门、模型通道未知时仅继续本地安全行为。测试使用假世界/假模型，不能称为已在真实服务器完成这些动作。

**当前行为对历史模型超时记录的补充：** 下文 14:14 的自动退避重试是当时版本行为。当时单模型门禁把超时/网络中断视为远端请求可能未结束，重启和 `--resume` 不自动清锁；确认服务端空闲后才可显式 `--acknowledge-model-idle`。完整 HTTP 429/5xx 响应仍可按策略退避，二者不能混淆。

真实日常验收还需覆盖食物、熔炼/铁器、建屋/返家/关门、储物、耕种后续产出及自然失败恢复。`maintenance_ready` 只说明当前任务板条件，4 株活作物的 `renewable` 只说明可维护生产点，均不证明长期产量或夜间安全。运行中的运维 `maintenance` 也不是日常任务板已达到 `maintain`。

历史开发检查点由 HTTP 核验为`paused_for_development`、0 步、`pause_reason=daily_survival_implementation`；`ready=false`、`connected=false`、`maintenance=true`，最后游戏观察仍为 `2026-11-14T06:19:29Z`。新增 daily 代码当时尚未加载到自主机器人。本段没有新增真实成功声明；正式恢复后须按新进程与新会话补充，不能把旧观察或维护页在线当成当前游戏验收。

## 历史：2026-11-14 树冠分段撤退与失败重规划

当时仍只修改 BotSleep，API 保持官方 1.3.7（`1cfecc3`）。只读采样确认旧树冠并非物理无路：存在上升1格、下降1格、下降1格、下降2格到草地的路线；原先单步最多下降1格和最终三维距离增加的约束排除了它。真实地形中规划实际读取的71个体素已提炼到 `tests/fixtures/retreat_canopy_steps.json`，不含账号、密钥或服务器标识；未采样坐标作为未知处理。

`python -B -m unittest discover -s tests -q`：**390 项通过，5.868秒**。新回归覆盖四次逐格重新规划（前三次 relay、最后 ground）、两格下降净空、未知地形、失败落点排除、无路等待后重搜、威胁暂时离开仍持续撤离、死亡/维度变化清理，以及最后一次动作前状态已被推动或不再落地时不发送移动。另外6项边缘起点回归检查碰撞箱确定接触、未知或水浸支撑、扫掠空间侧向遮挡、威胁限制、完整实地路线缺失及发送前接触变化。假 API 的路线闭环证明规划与验收逻辑，不代替真实路径执行。

13:47:40 和 13:49:41，只读检查期间机器人自然被蜘蛛击杀，随后重生到新的位置。因此旧现场四步路线仅作为离线回归，恢复后必须按新位置重新观察。13:59:58 仅替换 BotSleep 进程并显式 `--resume`，API 与游戏服务器未重启；会话从夜晚无床等待开始，21步包含16次等待和5次撤退拒绝，均无实际位移。新现场中心脚下为空，但角色在相邻白桦树叶边缘获得支撑；API 的一位小数位置与整个人物碰撞箱落地判定解释了这项差异。14:02 在空闲边界切换只读局部采样，14:05:28 检查期间又被骷髅自然击杀并重生；这些事件均不是撤退或模型技能成功。现场证据继续记录在忽略目录 `.test-runtime/live-canopy-verification-20261114.json`，过程没有改地图、发物、传送或调整时间/难度。

### 恢复运行后的模型服务阻塞（14:14）

14:09:40 当时加载全部修复并启动新会话。新位置 `[7.5,115,-4.5]`，生命和饥饿值均20；14:09:45 开始请求模型。至14:14，前三次正式请求均在60秒后返回 `llm_timeout`，第四次请求进行中。每次明确 `executed=false`，harness 自动退避、重新观察后重试，没有伪造工具结果或回退到规则采集。监控 HTTP 可访问，运行中且未暂停。14:15 复查时第4次请求也已超时，机器人仍未暂停，监控HTTP200。

为区分上下文过大与外部链路问题，一次仅32输出 token 的小文本探测在TLS1.2下也于20秒超时，自动TLS探测连接失败；同一配置下只读 `/models` 在0.421秒返回HTTP200，仅列出 `default`。这证明模型列表可达，但不能保证推理后端可用，也无法取得当前 `response.model`；没有改密钥、模型路由或 TLS 配置。

当时修复的390项离线回归已通过，历史记录中监控和自主进程保留运行并遵守既有预算继续重试；该自动重试行为已被后续模型门禁替代。**这段新会话尚无真实模型响应、库存增长、采集/制作成功或同会话技能失败后的模型调整；真实树冠分段退出也尚未验收。** 不能把之前版本的成功记录移作本次证明。旧树冠四段地形回归、边缘支撑判断与当前服务阻塞应分别评估。

## 历史：2026-11-14 API 1.3.7 适配与真实运行

相邻 API 保持官方 `1cfecc3`，没有本地源码补丁或依赖变更。13:16 重启 API 后，日志出现 `WORLD ready after 3675 ms`，BotSleep 轮询至 ready 后继续；真实 `/status` 已返回 `speed=0`、`velocity=[0,0,0]`，只读监控 HTTP 200，页面包含格/秒的速度显示。后台进程及日志入口见本地忽略文件 `.test-runtime/processes.json`。

离线覆盖包括：`dig_unconfirmed` 延迟确认并入库、已移除但未拾取、缓存原方块仍未知、必要读取和 stop 失败、取消、死亡变化、复核预算、原生失败反馈配对、暂停与 pending 持久化，以及 `--resume` 只重新观察、不重放意图。真实技能与 harness 联合回归还确认不会重复 stop 阻塞主循环。新错误码的离线模拟不等于真实服务器自然触发。

`python -B -m unittest discover -s tests -q`：**362 项通过，6.868 秒**。其中包括撤退时短草/落叶通行、有限台阶与树冠退出候选、跳跃/下降头部净空、读取/节点预算、终点变化和真实位置验证；原版 API 的路径执行仍不能由离线测试代替验收。

实际运行的限制也如实记录：最初新版会话受近敌避险门禁接管，6 次撤退均未找到同层安全通道，随后暂停，没有模型请求。服务器在 13:21:58 和 13:22:30 记录被骷髅击杀；重启 BotSleep 后的新会话从树冠 `[9.6,113,-9.7]` 登录，实际库存为空。之前在旧 API 下做出的木镐、石镐均不再持有。第二会话的阻挡诊断确认存在树叶支撑和缺地，不能把这两段运行计作新版采集成功，也不能把新登录的 `last_death=null` 当作没有死亡。

继续验收的脱敏证据写入 `.test-runtime/live-api137-verification-20261114.json`，按会话区分 API 版本、模型响应、库存增长、失败反馈及安全暂停。整个过程沿用普通世界，没有测试布场、发物、传送、改时间或难度。

### 前一轮现场结果：安全撤退阻塞（13:39）

13:36:37 只重启 BotSleep 加载有界台阶/树冠退出候选搜索，API 进程保持官方 1.3.7。新会话仍未找到满足支撑、净空和远离威胁要求的终点：每次搜索 13 个站位、91 次方块读取，`termination=no_safe_destination`，有限阻挡诊断含身体空间中的橡树叶和白桦树叶。13:39:15 第6次 `unsafe` 后自动暂停，没有执行未经核验的移动。

三段新版会话共18次撤退失败，均由本地近敌安全门禁触发，没有模型请求，不能计作模型根据失败反馈自主调整。最终位置仍 `[9.6,113,-9.7]`、背包为空，当时监控可访问，API 和机器人进程在线但自动行动暂停。**新版下自主采集、制作和模型失败恢复尚未通过实测**；仍需解决当前树冠上的安全脱困路线。日志未自然出现 `dig_unconfirmed`，该错误的恢复分支目前仅有离线和本机模拟证据，不能声称已在真实游戏触发并恢复。

诊断边界：`no_safe_destination` 表示不满足当前搜索条件，不能证明游戏中物理无路。当前威胁比机器人低约6.9格，要求最终三维距离再增加2格会排斥部分下降路线；现有诊断未区分“没有连通实地”和“实地被距离条件排除”。后续需先补充该分歧的证据，再决定路线策略，不能仅据4个阻挡格放宽净空或落脚检查。

## 历史：2026-11-14 原版 API 与天然地图继续验证

当时仅修改 BotSleep，验收使用 **`mc-agent-api` 提交 `e11f2f3` 的原版源码**，未修改其配置或依赖。后续检查或拉取的官方更新不属于本段已验证版本，更新后的兼容性与行为需另行核验。继续使用已有普通世界与生存模式，没有为验收发放物品、传送、改时间、改难度或布置人工采集场。

- 离线回归：`python -B -m unittest discover -s tests -q`，**317 项通过**（5.597秒）。新增保守体素视线、原版字段兼容、掉落位置反馈及制作未知响应等回归。
- 12:49:22 自主采集橡木原木成功，方块消失且库存 `oak_log` 增加1；12:50:05 制作木板0→4；12:50:41 制作木棍0→4、木板4→2。
- 12:51:28 再次制作木板因没有原木返回 `missing_ingredients`，没有虚增库存。随后在动作完成且 `pending=null` 的边界重启机器人加载 BotSleep 侧修复，API 进程保持不变。
- 新运行继承检查点中的失败上下文，12:53:38 模型选择补采另一块原木，库存0→1；12:54:15 制作木板2→6；12:54:35 合成工作台0→1；12:55:09 放置工作台 `[7,111,-5]`，世界方块匹配且工作台库存消耗1。
- 同一运行内，各次成功技能结果均以匹配 `tool_call_id` 回传下一轮，无孤立或未闭合调用。重启不恢复旧原生消息历史，因此材料不足→补采属于**检查点失败上下文恢复后的调整**，不声称这段跨进程记录是同一原生工具事务。

### 同一会话内的真实后端失败恢复

以下记录均来自新会话，中途没有重启。表中 UTC 时间加 8 小时即 Asia/Shanghai 当地时间。

| 时间（2026-11-14，UTC） | 事件与验证证据 |
| --- | --- |
| 04:56:40 | `craft_item wooden_pickaxe` 实际返回 `missing_ingredients`；库存为木板 2、木棍 4，没有虚报木镐增加。 |
| 04:56:47 | 下一模型请求包含与原调用匹配的失败 `tool_call_id` 结果，错误码仍为 `missing_ingredients`，无孤立或未闭合调用。 |
| 04:57:19 | 模型改选 `gather_block oak_log [6,114,-4]`，没有重复缺材料的制作操作。 |
| 04:57:27 | 采集成功：原木库存 0→1，`block_removed=true`，目标方块为 `air`。 |

这证明了**真实技能失败→同会话原生工具反馈→模型换行动→实际库存增加**。与之区分，04:55:43 的 `move_to [7,111,-5]` 因目标已在容差内，被本地以 `llm_no_progress` 在执行前拒绝；对应失败反馈后，04:56:36 模型改选制作木镐。这后一段证明的是本地调用校验与有界修复，不能计作后端动作失败恢复。

脱敏报告 `.test-runtime/live-survival-verification-20261114.json` 覆盖前后两次会话，共检查 12 个模型请求，原生工具消息配对均正确，并分别标注跨进程检查点恢复、本地调用修复和同会话后端失败恢复。

上述证据覆盖模型自主采集、基本制作、工作台放置与失败后调整；**完整石器工具链、食物获取及长期生存仍未验收**。模型名采用服务返回的 `qwen3.6-35b-a3b`，不保证 `default` 路由以后固定指向该型号。该历史会话的脱敏原始证据在忽略目录 `.test-runtime/llm-20261114T044359Z.stdout.log` 和 `.test-runtime/llm-20261114T045216Z.stdout.log`，不提交私密运行数据。

## 历史：22:52 寻路与模型反馈修复暂停点

- `python -B -m unittest discover -s tests -q`：293 项通过，4.456 秒；输出保存在忽略目录 `.test-runtime/navigation-offline-tests.log`。
- 当时实验性的 API 补丁通过了 13 项导航测试与语法检查；**2026-11-14 按用户要求已全部撤回，未提交或推送，当前不再包含该测试文件**。仅保留 BotSleep 侧适配和回归测试。
- 真实运行已确认 `default` 返回 `model=qwen3.6-35b-a3b`，模型调用了 move_to/gather_block，失败结果已进入反馈。原有寻路多次 stalled；原地 range 内移动未产生位移；历史膨胀后的请求实测 51,524 tokens 超过服务 43,776 上限并返回 400。
- 实际地形读取确认 `[6,112,-4]` 原木从机器人 `[4.3,109,-5.5]` 几何可触达但被土墙遮挡。修复增加可见性、站位候选、结果诊断与上下文预算；当前测试覆盖这些回归，**尚未完成修复后的真实自主采集、制作验收**。
- 用户准备断电，停止后续真机测试。机器人、监控和 API 已停；Minecraft 保存全部维度后正常退出。下次须由用户明确恢复，再使用原版 API 与机器人观察真实库存增长；原版 API 未提供的视线等元数据仍属观察限制。

## 历史：离线自动化回归

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

## 历史：独立真实游戏验收

当时使用 `.test-runtime/leaves` 内的独立世界，复制原目录的服务端二进制和依赖，不使用硬链接，不修改原服务器配置或存档。

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

用户随后要求进入查看、启动机器人并改成普通世界。当时 `.test-runtime/leaves/botsleep-survival-world` 为新随机种子 normal 世界，默认 survival、normal 难度、结构生成开启；原 `botsleep-test-world` 及下界/末地目录完整保留。Ink_bai 的 OP4 保留，未搬移旧玩家数据。生成器已经通过新 level.dat 确认是 `minecraft:noise` / `minecraft:overworld`，并非仅改 properties 的文字。迁移记录在 `.test-runtime/world_migration_report.json`，进程在 `.test-runtime/processes.json`。

## 验证范围

真实游戏验证了登录、正确床位查询、接近、交互、入睡、跳夜、醒来确认、无床失败和登出。

取消、危险维度、怪物、多床、断线、延迟状态包等边界已由离线测试覆盖，当时没有逐一在游戏中制造这些场景；没有验证长期生存、多人真实睡眠、插件自定义聊天或模型决策。

同步 HTTP 请求无法被 Python 取消信号即时打断；后端 `/stop` 也不保证所有动作立刻停止。当前控制器会停止后续动作，等待请求返回或超时，并在退出时登出。

## 历史：初期生存新增验证（同日）

当时源码 **197 项 unittest 全部通过**（历史结果）。新增覆盖六个生存动作的真实结果判断、材料扣除的规划模拟、参数/危险门禁、单动作互斥、执行前持久化意图、未知结果暂停与重启不重放、死亡回退与延迟包、扫描中死亡导致快照失效、手动请求忙态保护、步骤/运行时间预算、断线复用会话、世界隔离与损坏存档保护。37 个项目 Python 文件通过 AST 解析，`git diff --check` 通过。

真实游戏测试另建 `.survival-test-runtime`，使用 **25586 / RCON 25596 / API 18921**，账号 `SurvivalTestBot`，世界 `survival-smoke-world`。这是白天、和平难度的可破坏平坦测试场，泥土地板、24 个原木和24 个侧面石块。机器人从空背包开始，不授予物品；工作台必须自行合成并放置。运行真实 `main.py --mode survival --max-steps 80`，完成后以服务端保存的玩家 NBT 背包独立验算目标物品。

| 运行 | 结果 | 证据与恢复 |
|---|---|---|
| 首次预检 | 登录阶段失败，没有进入采集 | 暴露出生前 status 的精确 toFixed 初始化错误；客户端现于原登录 deadline 内重试该暂态，不泛化重试 internal |
| 初次完整流程 | 41步，120.92秒，退出0 | 石镐、石斧、石剑、熔炉各1；4次接近失败与1次未收集到掉落，调度器自行换目标，无补发材料 |
| 收紧寻路半径后的最终源码 | **38步，99.86秒，退出0** | 四件目标各1，工作台当时自制自放；原接近失败点直接成功，1次禁止挖自身上下列的保护拒绝后自行换目标完成 |

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

**这次 197 项版本的验收没有验证普通自然地图的长期自主生存**。当时食物获取、熔炼、建屋、战斗尚未实现；后续日常模块的实现及离线范围见本文顶部新增章节。饥饿/撤退/死亡恢复等边界主要由离线测试验证，不能将上述工具链测试视为这些能力的真实游戏验收。

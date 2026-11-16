# 配置、客户端、生存、赛事与监控验证

最近独立复核：2026-11-16（Asia/Shanghai），`HEAD 38db5d5` 加阶段候选、恢复工具、日志监控与本地执行优先与安静树冠分段下降工作树改动。首节为本轮结果；历史结果与实服现场不代表当前版本通过。

## 树冠身体高度已清空后仍无法下降（2026-11-16）

只读现场日志与原生监控确认：新架构已加载，机器人脚部 Y=92，附近已观察地面脚部约 Y=88，本地为 `no_verified_local_recovery`、0 steps。身体高度无合法清叶候选，旧 retreat 的完整实心终点与历史威胁方向约束仍会拒绝部分已核验的下降步骤，模型最终反复 wait/report。首次失败用例验证了：存在已核验侧下落脚点时，旧 `LocalExecutor` 仍返回 None。

新增 `core/canopy_geometry.py`、`skills/canopy.py`，collector 对安静树冠补充最多96读/3秒的有界采样，harness 专用 `descend_canopy` 可最多清一格侧下方干叶，单步下降1～2格；也可朝3格内已核验下降口水平推进1格。当前脚下支撑不挖，四格直落不放行。实际近敌仍使用旧 retreat；新路径独立复核源点和目标12格缓冲、生命周期、身体净空与支撑，不让历史威胁坐标阻断无近敌下降。模型工具仍9/33/38。

| 检查 | 本轮复核结果 |
| --- | --- |
| `BOTSLEEP_RUN_LIVE_TESTS=0` 下 `python -X utf8 -B -m unittest discover -s tests -q` | **1081项，44.095秒：1077通过、3项门户实服跳过、1项旧SQLite清理ERROR**；无其他失败，不能报告全绿。 |
| 新增树冠回归 | **44项全部通过**：15项几何/collector/harness整链，29项真实内部技能与可变内存世界回归。 |
| 整链 | 身体高度已清空、地面低四格的合成世界中，最多清两格侧叶，3步 `92→91→90→88` 到实心地面，零模型请求；原支撑叶保留。competition selection 阶段同样可先恢复，不选目标、不授分。此地形保留现场失败结构，不是原世界完整体素重建。 |
| 规划与活性 | 侧叶有下层支撑才可清理；最多3格直线内先证明有下降口才可水平中继；无出口不盲走，未知/含水/岩浆拒绝、失败目标排除、读预算有界。叶支撑本身足以标记树冠恢复，不依赖首4条拒绝证据中恰好出现叶片。 |
| 写入与落地 | 每次写前重查支撑、源点与实体；未知 dig 保留 pending、禁止 move/重挖；写后受伤/取消/重生均不续行。一次 move 后最多1.25秒/6次状态观察核验落地，浮空或危险不成功。目标脚格、Y误差与X/Z各轴安全足迹按API舍入复核，不要求无误差的数学中心。 |
| 兼容性 | 原本地优先81项、daily224项、retreat16项定向回归通过，亦包含于最终全量。示例配置、Python AST、文档相对链接及 `git diff --check` 通过；未增加模型调用入口或放宽通用 clear/force 权限。 |

唯一ERROR仍为既有 `test_corrupt_and_future_schema_databases_are_preserved` 的Windows SQLite临时文件清理WinError32/267；本轮未修改该模块或抑制异常。仅离线/mock/本机假HTTP，现场核查只读取日志与监控；未登录新游戏会话、重启或操控在线机器人、修改运行检查点、请求真实模型或使用RCON，亦未修改相邻mc-agent-api。原native监控确认过的在线进程尚未加载本轮修复；需要重启加载后另做实服验收，不能据合成下降成功宣称原位置已经脱困。

## 历史：Harness 本地执行优先与模型接管（2026-11-16）

默认启用 `survival.local_execution`，预算为64步/180秒。harness 先执行有证据的任务步骤、换可观察资源目标、清障/撤退和已登记熔炉等待；无支持计划、替代方案耗尽或预算耗尽才请求模型。成功本地步骤不等普通poll，主线程busy/本地ready最多50ms唤醒，聊天独立节流。每步仍经过新观察、安全门禁、原pending、串行真实技能和后验核验；未知动作/模型槽不因本地优先解锁。模型请求前提供最近最多6条结果摘要与失败目标。

| 检查 | 本轮复核结果 |
| --- | --- |
| `BOTSLEEP_RUN_LIVE_TESTS=0` 下 `python -X utf8 -B -m unittest discover -s tests -q` | **1037项，54.159秒：1033通过、3项门户实服跳过、1项旧SQLite清理ERROR**；无其他失败，不报告全绿。 |
| 新增本地优先回归 | **83项**：32项任务单步规划、27项执行器预算/状态/未知锁/进展、9项实际starter与树冠运行链、5项competition/daily生命周期、8项监控、2项配置。原模型协议夹具显式关闭本地优先，继续验证原生工具反馈。 |
| starter完整链 | 空背包开始，采集真实减少目标、制作扣材料、放置工作台、木镐后采石，取得三件石工具与熔炉并核验goal_complete；**32步、31次技能写动作、0次模型请求**。仅在受控内存世界验证，资源在交互范围内，不代表实服寻路性能。 |
| 局部恢复与接管 | 树冠单叶清障后重观测并完成两步撤退，零模型请求；一个资源不可达先换另一个已观察目标。无候选/无进展/预算耗尽交模型，现实不变不重启同一失败；已知熔炉加工由本地有界等待推进。 |
| 赛事证据 | 本地选择→扣材料制作→新鲜服务端advancement→finish，零模型请求；库存齐全不替代服务端完成。模型等待后收到新完成证据可回本地收尾；API证据不可用不finish、不授分。仍只有7个明确行动计划。 |
| 未知与监控 | 未知结果跨作用域变化仍保持本地blocked_unknown/pending，不重放；模型门禁uncertain不本地生产。只读面板显示本地阶段、原因、步数、近期结果和决策来源；摘要/接管事件不伪造模型在途。 |
| 配置与静态 | 示例Configuration valid；118个Python文件AST、文档相对链接与 `git diff --check` 通过。默认预算与示例一致，显式 `enabled: false` 可恢复逐步模型规划。 |

唯一ERROR为既有 `test_survival_memory.SurvivalMemoryTests.test_corrupt_and_future_schema_databases_are_preserved` 的Windows临时SQLite文件清理WinError32/267，本轮未改该模块或抑制异常。3项跳过不是实服通过。只使用mock、内存游戏和本机临时假HTTP；没有真实模型/API/RCON请求，没有重启在线机器人或写其运行检查点，没有修改相邻mc-agent-api。当前运行进程需重新加载代码后另做实服验收；本轮不能证明完整比赛执行或长时无人值守完成。

## 历史：小模型空参/多call：观察绑定恢复（2026-11-16）

最新实测已加载anyOf参数修复，但模型持续返回force_attempt `{}`或两个call；report后又退回同一模式。另有旧撤退搜索冷却在模型思考期间到期，执行前强制搜索覆盖已经核验的清障。该层修复仅增强schema不足，需要降低模型所需参数量。

实现临时请求工具 `force_clear_foliage`：选择一格当前可见、相邻身体高度、明确非含水、非支撑且不在冷却/相同失败状态的叶片，描述中绑定名称和坐标，模型 `{}` 或仅reason即可表示显式选择；不从reason补参，不注册全局通用动作、不改变永久9/33/38目录。canopy只提供该工具/report/wait，selection亦可先脱困；无合法目标/明确危险不提供绑定动作。模型原alias/call_id仍配对canonical force结果，执行前再次复核。HTTP明确parallel_tool_calls=false，本地仍拒绝多个call。

| 检查 | 本轮结果 |
| --- | --- |
| `BOTSLEEP_RUN_LIVE_TESTS=0` 下 `python -X utf8 -B -m unittest discover -s tests -q` | **954项，37.415秒：950通过、3项门户实服跳过、1项旧SQLite清理ERROR**；无其他失败，不能报全绿。 |
| 新增回归 | **11项**：9项pure planner绑定规则/目标冷却/别名配对/额外字段拒绝/危险与完整观察/无registry污染；2项实际collector/harness/skill链中目标变更拒绝与未选赛事任务先恢复。原完整树冠执行链已改为alias `{}`并模拟旧冷却到期，不增加该用例计数。 |
| 完整离线执行链 | alias `{}`清一格叶片后重观测并续接两步撤退到实地；冷却在模型等待时到期不抢占；目标在回应前变stone则无dig/place/move且无pending；selection阶段可执行恢复而不授分。 |
| 真实输入离线重放 | 从已有本地请求中重放树冠观察/约束/失败冷却，使用假Brain返回alias `{}`，选择另一合法树叶、跳过冷却目标。只重放输入，未探测模型或向游戏派发动作。 |
| 配置/监控/静态 | 示例Configuration valid；110个Python文件AST、84个相对文档链接、git diff --check通过；本机18930原生监控与18931只读日志查看器都响应，来源/心跳分别展示。 |

唯一ERROR仍为Windows SQLite临时文件清理WinError32/267，未修复或抑制；3项实服跳过不是脱困成功证据。仅离线模拟/本机假HTTP和读取已有监控页面，没有新游戏/API/RCON/真实模型请求、没有重启机器人或清锁。线上旧进程仍会按旧工具schema运行，加载此修改后是否实际脱困需另行验收。

## 历史：force必填参数反馈与只读日志监控（2026-11-16）

现场日志重复返回 `force_attempt {action:clear_foliage,reason:包含坐标的文字}`，遗漏JSON pos/expected_name；旧schema仅把action/reason标必填、内部再校验却反馈笼统，模型未修正。本轮增加按真实action required声明的anyOf分支与字段说明；本地仍按真实schema校验，明确missing_keys/unsupported_keys和JSON字段形状，不从reason猜坐标。连续两次格式/不可用工具错误后下轮提供report_blocked，防止继续重复畸形请求。

监控打不开的已核对原因是私有配置 `debug.monitor.enabled=false`。本轮仅把该开关改为true供下次正常启动，未重启机器人。新增 `debug.monitor_tail` 在独立18931端口补读现有flight日志，重用只读HTTP、脱敏和缓存限制；启动后实际 `/` HTTP200、`/api/state` source.mode=log_tail 且已有日志数据，原日志无HEARTBEAT时仍为null。查看器存活不证明机器人在线。

| 检查 | 本轮结果 |
| --- | --- |
| `BOTSLEEP_RUN_LIVE_TESTS=0` 下 `python -X utf8 -B -m unittest discover -s tests -q` | **943项，49.342秒：939通过、3项门户实服跳过、1项旧SQLite清理ERROR**；无其他失败，不能报告全绿。 |
| 新增回归 | **26项**：23项日志尾读（有界起始窗口/坏行/超长行/UTF8拆分/追加/截断/轮转/跨会话隔离/不伪造心跳等），3项force必填schema/原样错误重放修正/连续畸形转report。 |
| 当前只读页面 | 本机18931启动并独立HTTP复核成功，不读私有配置、不登录游戏、不发模型请求；主程序未来仍用18930，避免端口冲突。HTML来源标识已在原面板脚本测试覆盖。 |
| 静态检查 | 109个Python文件AST、82个相对文档链接、git diff --check通过；本机配置加载确认monitor.enabled=true。 |

唯一ERROR仍是Windows SQLite临时清理WinError32/267。仅读取本地事件诊断和mock/本机假HTTP测试，未操作游戏/API/RCON/模型、未写用户运行检查点或清锁。只读日志查看器作为后台辅助进程保持运行；后续加载机器人新代码时要正常停止旧进程再启动，不并行第二个同名客户端。已有进程不会热加载参数反馈修复。

## 历史：required工具、受阻报告、强制尝试与树冠恢复（2026-11-16）

用户明确同意分开实现required工具输出、report_blocked和force_attempt；强制仅覆盖没有明确危险的保守策略，如夜间单次采集/相邻清叶，不解除未知动作、模型门禁或实际目标/材料/几何限制。注册工具starter9/daily33/competition38，按本轮安全约束和任务phase动态过滤；预算计算采用实际发出的tools，原registry不变。

| 检查 | 本轮结果 |
| --- | --- |
| `BOTSLEEP_RUN_LIVE_TESTS=0` 下 `python -X utf8 -B -m unittest discover -s tests -q` | **917项，33.865秒：913通过、3项门户实服跳过、1项旧SQLite清理ERROR**；无其他失败，不能报告全绿。 |
| 新增恢复工具/传输/落地回归 | **43项**：required HTTP5项、pure policy/planner14项、落地确认16项、daily真实离线链6项、competition受阻退出2项。 |
| required与动态目录 | 有tools请求required，无tools无tool_choice；无auto降级/自动网络重试。实际provider工具按allowed_actions与任务phase过滤；隐藏工具被有界repair拒绝且反馈配对。连续3次同任务同状态wait仅提供report_blocked，真实库存/任务变化解除。 |
| force与blocked真实离线链 | 夜间19血单次采木库存+1/目标air；夜间强制清一格相邻叶片且collect=false；近敌出现拒绝写入；超时/写后受伤保留pending且不重放。活动任务连续3次同状态blocked自动强退cooldown，显式exit_task立即回选择，不授分、不清失败。 |
| 树冠与撤退 | 已记录blocked canopy升级完整观察，模型force清一格后重读地形并续接两次检查后的撤退、到达实地清恢复意图；不是回放旧route。move_to只发一次，最多1.25秒/6次读等精确目标格落地；浮空/错格/危险/重生不算成功，记录实际位移和原因。 |
| 配置与静态检查 | 示例Configuration valid；107个Python文件AST、79个文档相对链接、git diff --check通过。 |

唯一ERROR仍为 `test_corrupt_and_future_schema_databases_are_preserved` 的Windows SQLite临时文件清理WinError32/267，未修复或抑制。3项跳过不是实服通过。只使用mock/内存游戏/本机临时假HTTP，未发真实模型/API/RCON请求、未重启在线机器人、未写其运行检查点，也未修改相邻API。required只约束provider输出，不能保证模型提案正确；force_attempt也可因新证据失败，不能据离线通过宣称任意树冠可脱困或长时无人值守已验收。

## 历史：无床黄昏/夜间开局修复（2026-11-16）

实测日志显示模型已选石器任务并提出采5根木头，但复查时跨过11000tick，旧本地策略返回 `night_requires_verified_sleep_or_shelter_route`；随后多轮本地wait，不是模型无响应或上下文超限。本轮用完整合成观察重现：满血/满食物且附近可见原木时，黄昏和夜间均 model_allowed=false、choose=wait。

新增 `night_preparation` 在无现成床/已验证返家路径、状态与附近证据满足保守阈值时开放3格内单步准备；夜间仍禁止批量、探索、狩猎和清叶，不修改底层技能安全契约。模型返回后的同一门禁重新核验；在途受伤、雷暴、食物不足或敌人进入缓冲会取消/停止，迟到提案不执行。详见 [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md)。

| 检查 | 本轮结果 |
| --- | --- |
| `BOTSLEEP_RUN_LIVE_TESTS=0` 下 `python -X utf8 -B -m unittest discover -s tests -q` | **874 项，43.333 秒：870 通过、3 项门户实服跳过、1 项旧 SQLite 清理 ERROR**；无其他失败，不能报告全绿。 |
| 新增夜间回归 | **19 项通过**：12项纯策略、6项真实collector/harness/skill + 本机假HTTP、1项赛事生命周期夜间元数据。 |
| 真实离线执行链 | 无床夜间单次采木后原木库存+1且目标变air；已有工作台放置后库存-1且目标匹配；单步提案跨黄昏可执行；批量提案跨黄昏拒绝、配对失败反馈后模型改单步成功。 |
| 危险/恢复边界 | 等待模型时生命下降、11格敌人或雷暴触发取消；无dig/place/move写入、没有第二个模型请求、迟到提案排空；规则模式不能绕过单步白名单。原床/返家/室内、低血、饥饿、未知状态等回归仍通过。 |
| 配置与静态检查 | 示例Configuration valid；103个Python文件AST、75个文档相对链接、git diff --check通过。 |

唯一 ERROR 仍是 `test_corrupt_and_future_schema_databases_are_preserved` 清理临时 SQLite 的 Windows WinError32/267，未修该模块或忽略异常。3项实服跳过不可计作游戏验收。没有连接真实模型/API/RCON、改时间、给物或重启当前机器人；仅离线模拟时序与读取此前日志。运行中的旧进程需重启加载代码后独立验收，本轮不声称实服已恢复行动。

## 历史：状态驱动阶段候选与上下文超限回归（2026-11-16）

用户实测发生 `llm_context_limit`；本地日志确认首轮当前输入约51–53KB，预算48KB，未发送模型请求。离线合成密集观察+旧全目录的复现为57,801字节，历史为空且模型假客户端未被调用，排除了“仅历史太长”。根因是每轮125项目录+36工具定义+真实观察/恢复状态叠加；原预算用例只有稀疏观察，覆盖不足。

实现改为 `CompetitionTasks.model_view()`：选择时仅发送当前资源/附近环境解锁的候选摘要，选中后才发送完整单任务规则、缺口、最多5步建议、技能需求与完成/失败/中止判据；同一门禁控制 select，不能猜ID绕过。snapshot 保留125项用于本地计分和只读监控。阶段不会注册尚未实现的末地/下界技能。当前/历史消息与反馈移除JSON空白，仍以实际ASCII转义字节预算校验，不抬高上限；不是语义compact。

| 检查 | 本轮结果 |
| --- | --- |
| `BOTSLEEP_RUN_LIVE_TESTS=0` 下 `python -X utf8 -B -m unittest discover -s tests -q` | **855 项，30.678 秒：851 通过、3 实服跳过、1 旧 SQLite 清理 ERROR**。无其他失败，不能报全绿。 |
| `test_competition_task_view.py` | 新增10项通过：资源阶段变化、可选摘要/详情分离、全部合格候选不截top-N、隐藏目标选择拒绝、完整condition、冷却、unknown和terminal。 |
| 实际 harness → worker → planner 多轮预算回归 | 密集48方块/16地形/12实体/站位候选与中文召回，8轮请求42,540–47,359字节，均小于48,000；选择后只发活动任务详情，完整审计目录仍为125项，最近工具反馈ID保持配对。 |
| `python -B -m unittest discover -s tests -p test_llm_planner.py -q` | 48项通过；超大输入仍在本地拒绝，不绕过模型门禁。 |
| 示例配置与静态检查 | Configuration valid；102个Python文件AST、69个文档相对链接和git diff --check通过。 |

唯一 ERROR 仍为 `test_corrupt_and_future_schema_databases_are_preserved` 在 Windows 清理 SQLite 临时文件的 WinError32/267，本轮未改记忆模块。3项跳过是门户实服测试，不是本轮真实技能通过。仅重放读取已有本地脱敏事件/检查点并用合成夹具验证，没有新游戏/API/RCON/模型请求，没有清锁、重启机器人或写运行检查点；线上是否恢复需加载新代码后独立验收。运行中的进程不会自动热加载此修改。

## 历史：拉取后复核（2026-11-15，3801be9）

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

- 历史合并记录 `python -B -m unittest discover -s tests -q`：**772项通过，17.137秒，0跳过**；不替代本文首节772项中1项ERROR的结果。包含740项日常/原有回归、31项要塞模块测试及1项合并工具目录回归。
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

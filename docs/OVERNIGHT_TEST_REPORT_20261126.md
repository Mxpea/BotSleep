# BotSleep v2 过夜测试与代码覆盖报告（2026-11-26）

- **测试分支与基线**：`develop-tianya/rewrite-v2`（实服起测提交 `5e176a8` / `e528d86` / `cf39ed1` → 实服修复 `5bd6766` / `c53d3ad` / `c696764` → 9-Bot 复盘修复终态提交 **`7b14b12`**）
- **运行环境**：Windows 11 / Python 3.13（`.venv`），Leaves `1.21.11`（`difficulty=easy`、每轮重置存档），官方未修改 `mc-agent-api` `1.3.19`（`http://127.0.0.1:18900/api/v1`，`git diff --stat` 为空）
- **关联文档**：Token 账单与 API 异常明细见 [API_AND_TOKEN_REPORT_20261126.md](API_AND_TOKEN_REPORT_20261126.md)；回归台账见 [TESTING.md](TESTING.md)

---

## 1. 总结论

| 维度 | 结果 |
| --- | --- |
| **离线回归（`tests_v2`）** | **`77 / 77` 通过**（`33.727s`，0 失败、0 跳过；最终覆盖率跑完 `77 / 77` 在 `105.651s` 通过） |
| **`v2/` statement/line 代码覆盖率** | **`6,529 / 8,089` 可执行行 = `80.71%`**（覆盖全部 62 个源码文件） |
| **单 Bot 实服迭代（R1 / R1b / R2 / R3）** | 4 轮累计 `1,617.8s`（~27 min），仅用主路 `qwen3.6-35b-a3b`；定位并根因修复 4 类卡死/解析缺陷，R3 跑满 8 分钟零卡死零崩溃 |
| **9-Bot 分散压测（`v2_live9`）** | **9 / 9 全员正常退出 `rc=0`**（1800s 上限，合计 **4.50 Bot·小时**），0 Python 异常退出；服务端成就 **21 项**（`9/9` 石镐、`2/9` 铁锭、`1/9` 铁镐）；新捕获 2 处隐形卡顿/遥测缺口并已在 `7b14b12` 修复回归 |

> **实服与离线边界声明**：离线 `77/77` 通过与 `80.71%` 覆盖率使用进程内 `VirtualMcApiServer` + Mock LLM，不能替代实服长跑；9-Bot 30 分钟实服跑出了 `铁镐` / `打火石` / `水桶` / `enter_nether` 启动阶段，但 **`30 min` 内尚无 Bot 进入下界（`0/9`）**，且 `7b14b12` 针对 `bot_6` 原地空转与 `L4` 无限清零的修复已通过新增离线单测验证、尚待下一轮实服长跑复测。

---

## 2. `v2/` 代码覆盖率报告

因测试环境未安装第三方 `coverage` 包，采用纯标准库脚本 `scratch/measure_v2_coverage.py`：在 `tests_v2` 全套运行期间通过 `trace.Trace(count=True)` + `threading.settrace` 采集主线程及异步后台线程（`AsyncLLMReplanNode`、`UnifiedWorldDB`、`HttpDualPoolClient`）执行行，并以 `dis.findlinestarts(compile(...))` 递归统计 `v2/` 全部 62 个 `.py` 文件的字节码可执行行作为分母。

### 2.1 分层/分模块覆盖明细（`7b14b12` 终态，77/77 OK）

| 模块 / 层级 | 文件数 | 可执行行数 | 已覆盖行数 | 覆盖率 | 主要未覆盖区域说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| `v2/__init__.py` | 1 | 3 | 3 | **100.00%** | — |
| `v2/domain.py` | 1 | 122 | 116 | **95.08%** | 少数边界辅助分支 |
| `v2/observability` | 5 | 221 | 189 | **85.52%** | Monitor HTTP/WS 客户端异常处理边界 |
| `v2/planner` | 9 | 902 | 759 | **84.15%** | 真实 TLS1.2 socket 分支、部分 `deps_explainer` 模板 |
| `v2/bt` | 11 | 1,288 | 1,076 | **83.54%** | 部分夜视/极端跌落恢复分支 |
| `v2/gateway` | 6 | 1,029 | 853 | **82.90%** | `multihop_navigator.py` 船只/极端 3D 重试路径 |
| `v2/tasks` | 12 | 1,644 | 1,343 | **81.69%** | `shelter.py` 四向建屋明细、`stronghold_end.py` 末地终局分支 |
| `v2/kernel` | 6 | 1,860 | 1,464 | **78.71%** | `client.py` 个别 HTTP 重试与长参数防御分支 |
| `v2/primitives` | 10 | 733 | 560 | **76.40%** | `combat.py`（未全链路实战）与 `container.py`/`utility.py` |
| `v2/app.py` | 1 | 287 | 166 | **57.84%** | CLI 实服主循环/控制台输出分支（离线 Cutover 单测只走 `max_ticks` 子集，其余在实服验证） |
| **总计 (`v2/`)** | **62** | **8,089** | **6,529** | **80.71%** | — |

### 2.2 覆盖率最低的单文件 Top 8

1. `v2/primitives/combat.py`：`21 / 71`（`29.58%`）
2. `v2/tasks/shelter.py`：`19 / 44`（`43.18%`）
3. `v2/tasks/stronghold_end.py`：`20 / 43`（`46.51%`）
4. `v2/app.py`：`166 / 287`（`57.84%`）
5. `v2/planner/deps_explainer.py`：`19 / 29`（`65.52%`）
6. `v2/gateway/multihop_navigator.py`：`234 / 356`（`65.73%`）
7. `v2/primitives/utility.py`：`38 / 57`（`66.67%`）
8. `v2/kernel/client.py`：`522 / 766`（`68.15%`）

---

## 3. 实服运行总账（单 Bot + 9-Bot）

### 3.1 单 Bot 迭代验证轮次（仅主路 AI，关闭溢出）

| 轮次 | Objective / Intent | 时长 | 最大 Tick | 关键成果 | 暴露问题与状态 |
| --- | --- | ---: | ---: | --- | --- |
| **Round 1** (`single_comp_r1`) | `competition` / default | `290.55s` | `280` | 暴露比赛工具提示词格式与路径解析 Bug | `HorizonSelector.format_top()` 误拼 `x1` 后缀导致 114 次 `ineligible_or_unknown_task`；已在 `5bd6766` 修复 |
| **Round 1b** (`single_comp_r1b`) | `competition` / default | `274.41s` | `160` | 实服获 **2 项赛事成就**（`mine_stone` + `upgrade_tools`，**`comp_score=10`**），切入 `iron_ingot` | 1）`pillar_jump_launch` 后因 `app.py` 1s 轮询休眠导致落地后原地连跳 6 次；2）空闲时模型连续 4 次空喊 `update_progress`；已在 `c53d3ad` 修复 |
| **Round 2** (`single_daily_r2`) | `daily` / `speedrun` | `561.73s` | `320` | 合成 `wooden_pickaxe`（`t=12`）、`stone_pickaxe`（`t=67`），阶梯下矿至 `y=44` | 1）模型回传 `item="furnacex1"` 被 `IntentCompiler` 拼成 `furnacex1x1` 连失败 4 轮；2）`y=44` 踏入地下暗水池被 `LocalRecoveryBranch`（`recovery_swim:swam`）永久锁死，抢占规划并溺亡；已在 `c696764` 修复 |
| **Round 3** (`single_daily_r3`) | `daily` / `speedrun` | `491.09s` | `140` | **完整跑满 480s 上限 `rc=0`**；木镐(`t=23`)→石镐(`t=40`)→熔炉(`t=79`)→煤矿(`t=91`)→烧铁/铁镐(`t=100..140`)，`hp=20.0` | **0 卡死、0 崩溃、0 `furnacex1x1` 错误**，验证 R2 修复生效 |

### 3.2 9-Bot 分散出生 30 分钟双 API 压测（`v2_live9`）

- **配置**：`daily` + `speedrun`，`max_runtime_seconds: 1800`，`max_steps: 2000`，`--spread --min-dist 1500`（实测 `min_dist=1731.1`），`llm.overflow.enabled: true`（仅本压测模板启用，`config.yaml` 仍保持 `false`）。
- **整场稳定性**：**9/9 在 `1800.24s–1808.78s` 全部以 `rc=0` 正常登出退出**（总 Bot 时长 `16,228.6s` = `4.51` Bot·小时），**`stdout` 未捕获异常 0 例**。
- **服务端成就与死亡（Leaves 日志 `logs/v2_live9/leaves_server.log` 精确统计）**：
  - 总计成就：**21 项**（`9/9` 达成 `[Stone Age]` 与 `[Getting an Upgrade]`；**`2/9` 达成 `[Acquire Hardware]`（`SleepBot5`, `SleepBot7`）**；**`1/9` 达成 `[Isn't It Iron Pick]`（`SleepBot7`）**）。
  - 总计死亡：**6 次**（`SleepBot6` 溺水 1，`SleepBot4` 僵尸 1，`SleepBot2` 僵尸 1，`SleepBot7` 骷髅 1，`SleepBot9` 溺尸 2；`SleepBot1/3/5/8` 全场 **0 死亡**），全部在 `WORLD ready`（p50 `3.52s`）后自动恢复继续任务。

| Bot | 运行时长 | 末 Tick | 末 HP | 末阶段 / 目标 | 服务端成就 | 死亡数 | 主要观察问题 |
| --- | ---: | ---: | ---: | --- | --- | ---: | --- |
| `SleepBot1` | `1800.46s` | `740` | `20.0` | `acquire:iron_pickaxex1` | Stone Age, Upgrade | 0 | `move_to:path_not_found` (20) |
| `SleepBot2` | `1801.39s` | `700` | `5.9` | `mine:raw_ironx4` | Stone Age, Upgrade | 1 | `move_to:path_not_found` (73) |
| `SleepBot3` | `1800.29s` | `740` | `20.0` | `acquire:flint_and_steelx1` | Stone Age, Upgrade | 0 | **50 次丢弃式异步重规划 + 28 次 `place:no_support`** |
| `SleepBot4` | `1801.71s` | `720` | `15.2` | `acquire:water_bucketx1` | Stone Age, Upgrade | 1 | 偶发 `move_to:timeout` (9) |
| `SleepBot5` | `1807.64s` | `700` | `4.2` | `acquire:iron_pickaxex1`（曾跑 `flint_and_steel` / `water_bucket`） | Stone Age, Upgrade, **Acquire Hardware** | 0 | 地下寻路 `path_not_found` (56) |
| `SleepBot6` | `1805.09s` | `760` | `20.0` | `acquire:raw_ironx8` | Stone Age, Upgrade | 1 | **`t=260..760` 原地 `[4958.7, 48.0, -102.5]` 空转 500 步（见下根因）** |
| `SleepBot7` | `1800.24s` | `740` | `11.2` | `acquire:iron_pickaxex1`（曾挂载 `enter_nether` / `water_bucket` / `flint_and_steel`） | Stone Age, Upgrade, **Acquire Hardware**, **Iron Pick** | 1 | 28 次有效规划 + 1 次骷髅击杀恢复正常 |
| `SleepBot8` | `1803.03s` | `620` | `20.0` | `acquire:iron_pickaxex1` | Stone Age, Upgrade | 0 | 稳定下挖至 `y=16` |
| `SleepBot9` | `1808.78s` | `840` | `3.3` | `mine:raw_ironx8` | Stone Age, Upgrade | 2 | 夜间/水边遭 Drowned 连续阻断（`move_to:timeout` 60、`interrupted` 16） |

---

## 4. 发现的根因问题、修复与遗留风险

### 4.1 本夜已定位并根因修复（均已合入 `tests_v2` 回归）

1. **比赛任务 `x1` 后缀拼接与空闲文字汇报空转**（`5bd6766`、`c53d3ad`）：
   - **现象**：R1 模型连续 114 次调用 `select_task` 全被拒；R1b 连续 4 次只发 `update_progress` 不选任务。
   - **根因**：`HorizonSelector.format_top()` 误把 `x1` 拼进提示词任务 ID；`IntentCompiler` 未阻止空闲状态反复 `update_progress`。
   - **修复**：清理提示词格式化、加入 `selectable_sample` 纠偏、增加空闲连续 `update_progress` 拒收。
2. **柱体垫脚起跳后被 1s 轮询休眠错过顶点**（`c53d3ad`）：
   - **现象**：R1b 连续 6 次 1ms 原地起跳失败（`nav:progressed:control`）。
   - **根因**：`control(jump=True, wait=False)` 后主循环按 `1.0s` `poll_interval_seconds` 休眠，醒来时 Bot 已落回地面。
   - **修复**：`v2/primitives/navigate.py` 对 `pillar_jump_launch` 置位 `fast_tick_once` 使 `v2/app.py` 缩减下一拍休眠至 `0.18s`，同时 `MultiHopNavigator` 对落地无进展跳边立即拉黑 `(p_target -> p_target+1)`。
3. **`LocalRecoveryBranch` 地下暗水无限游泳锁死 & `run_goal` 物品后缀叠拼**（`c696764`）：
   - **现象**：R2 `SleepBot1` 从 `t=180` 到 `t=320` 永久卡在 `recovery_swim:swam` 并最终溺亡；此前连调 4 次 `acquire:furnacex1x1`。
   - **根因**：`LocalRecoveryBranch` 在无目标且不缺氧时仍永续返回 `Status.RUNNING` 屏蔽 `AsyncLLMReplanNode`，且无头顶石顶破拆/岸边选向；`IntentCompiler` 直接拼接 `f"acquire:{item}x{count}"` 未剥离模型回声 `x1`。
   - **修复**：`v2/bt/branches/local_recovery.py` 增加岸边优先方向、受阻 2 拍自动挖除头顶阻塞块、第 3 拍无位移带 8s 冷却让出 `Status.FAILURE`；`v2/bt/branches/hard_safety.py` 低氧受阻同加破顶逻辑；`v2/planner/intent_compiler.py` 新增 `_normalize_item_and_count()`。
4. **`MultiHopNavigator` 零位移 `move_to` 伪进展 + `PhysicalUnstuckBranch` L4 无条件清零（`v2_live9` `bot_6` 17 分钟空转根因）**（`7b14b12`）：
   - **现象**：`bot_6` 从 `t=260` 到 `t=760`（`728s–1793s`）稳居 `[4958.7, 48.0, -102.5]` 一动不动，却未被 Watchdog 中断任务。
   - **根因**：`MultiHopNavigator` 3D 步在 `stalled=True` 时仍清零 `_stall_counts[key]` 且对 0m 位移 `move_to` 返回 `PROGRESSED`；同时 L4 重定位即便 `timeout`/`BLOCKED` 原地失败也清零 `unstuck_level=0`，导致 L1→L4→0 无限循环。
   - **修复**：`v2/gateway/multihop_navigator.py` 在 `stalled=True` 时保留计数，连续 6 拍 `stalled` 立即双写拉黑目标并返回 `BLOCKED(stalled_no_displacement)`；`v2/bt/branches/unstuck.py` 在连续 2 次 L4 重定位无位移失败后置 `NEEDS_AI`（`unstuck_l4_exhausted`）唤醒 AI 换目标。
5. **丢弃的异步规划未记入 `LLM_RESPONSE` 导致日志与账单不一致**（`7b14b12`）：
   - **修复**：`OpenAIBrain._call_single` 现每次 HTTP 成功即通过可选 `recorder` 记录无损 `LLM_HTTP` 事件，彻底消除观测遗漏。

### 4.2 尚未完全解决的隐形/待观察问题

1. **矿洞窄巷 `place:no_support` 高频失败**：`bot_3` 出现 `28` 次 `no free spot with solid ground within 3 blocks`。1×2 矿道中工作台/熔炉附近缺少带实心底面的空位时，`PlacePrimitive` 尚缺“先往旁挖开 1×1 壁龛或后退至开阔格再放置”的自适应。
2. **地下复杂路径耗时较长影响 30 分钟进下界率**：`bot_3` / `bot_5` / `bot_7` 已摸到 `flint_and_steel` / `water_bucket` / `enter_nether`，但因矿洞内 `move_to:timeout`（8s）+ 往返找水/砂砾耗时，30 分钟内尚未完成建门点火（下轮可在 `7b14b12` 上验证 60–90 分钟压测）。
3. **非目标掉落名称偶发纠偏**：模型偶发调用 `acquire:iron_ore`（1.17+ 默认掉落 `raw_iron`），虽然 `unknown_production_route` 很快纠正，可在 `IntentCompiler` 补充非精准采集态常见别名提示减少无谓 1 轮试错。

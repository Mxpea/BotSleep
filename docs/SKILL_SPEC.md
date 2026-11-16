# BotSleep 技能规范文档 (SKILL SPECIFICATION)

源码核对：2026-11-15，`3801be9`。本文区分模型已绑定技能、独立开发技能和未接入提案；实现存在不等于实服验收。验证结果见 [TESTING.md](TESTING.md)。

---

## 一、技能基础契约 (BaseSkill Contract)

单一宏技能采用 `skills/__init__.py` 的 `BaseSkill` 契约，客户端由构造函数 `BaseSkill(client)` 注入，生命周期钩子为：

- `validate_preconditions(**kwargs) -> bool`：校验动作前置条件。
- `execute(**kwargs) -> SkillResult`：核心动作逻辑。
- `rollback(reason: str) -> None`：异常中断时的恢复钩子，基类默认不执行动作；具体恢复以各技能实现为准。

`SkillResult` 包含 `success`、`message`、`data`、`error_code`。生存基础动作通过 `SurvivalSkills.execute(name, args, cancel_event=None, *, recovery_context=None)` 分派；`recovery_context` 由 harness 内部提供，不是模型参数。当前运行时为 `main.py` 启动的 `core/survival_harness.py`，不使用 `agent/loop.py` 旧骨架。starter/daily/competition 分别公开9/31/36项模型工具，仅接受已注册动作；实际契约见 [SURVIVAL.md](SURVIVAL.md) 与 [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md)。

---

日常生产技能及验证边界见 [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md)。下面的 `pillar_up`、`mlg_water` 及其 `telemetry.ReflexDaemon` 触发机制是未接入的设计规范，不属于可执行模型工具。

## 二、核心技能规范

### 1. `ContinuousRadarSkill`（独立开发雷达）

`ContinuousRadarSkill(client, memory=None, radar=None, min_move_distance=6.0, scan_radius=24)` 位于 `skills/exploration.py`。`enable()` 创建 daemon 线程，约每秒读取状态，首次或位移达到阈值时扫描；`disable()` 请求退出，但不 join 线程或保证在途 HTTP 已结束。`execute(force_sweep=True)` 执行一次读取和写开发记忆。

主入口只有同时启用 debug 和 `--radar` 才会创建并启用它，聊天可用 `#radar on/off/scan/poi`。这不是模型工具、路径规划器或只读网页监控。默认向 `data/game_logic/` 写地标、航迹和事件，其中部分文件受版本控制；该存储不替代 scoped SQLite 或生存检查点。

---

### 2. `pillar_up` (自动垂直搭高)

* **功能**：在当前位置向上跳跃并在脚下放置实心方块，使角色垂直升高 $N$ 格。
* **参数**：
  - `height: int = 3`：目标搭高层数（建议 1~10）。
  - `block_filter: Optional[List[str]] = None`：允许消耗的方块类型列表。
* **前置条件**：
  1. 背包中实心方块总数 $\ge height$。
  2. 头顶垂直 $height + 2$ 格空间内无阻碍。
* **错误码**：
  - `insufficient_blocks`：方块储备不足。
  - `overhead_blocked`：头顶空间受阻。

---

### 3. `mlg_water` (自动落地水 / 高空坠落缓冲)

* **功能**：角色在高空坠落时，在着地前最后一刻向地面倒水抵消跌落伤害，并在着地后立即收回水源。
* **触发机制**：由 `telemetry.ReflexDaemon` 线程以 20Hz 频率实时守候。
* **激活条件**：
  1. 角色处于无支撑下落状态 (`in_air = True`, `velocity_y < -12`)。
  2. 预估着地距离处于危险高度（距下方面积 $< 3$ 格）。
  3. 背包或手持栏存在 `water_bucket`。

---

### 4. `auto_sleep` (智能找床睡眠)

* **实现状态**：已实现通过公开 HTTP API 执行的有界状态机，离线测试与独立真实游戏验证范围见 [TESTING.md](TESTING.md)。自动搭高和落地水仍为未接入骨架。
* **功能**：仅在主世界的夜间或雷暴时搜索床、接近并入睡，等待服务器将机器人唤醒，依据状态变化验证完成。已在睡眠中的机器人直接等待醒来，不重复点击床。
* **调用契约**：

  ```python
  execute(radius=16, threat_check=True, *, timeout_seconds=600,
          move_timeout_seconds=15, confirm_timeout_seconds=5,
          threat_distance=8, poll_interval_seconds=0.5, cancel_event=None)
  ```

  - `radius`：1–64 格搜索半径。
  - `threat_check`：是否在移动前和接近床后检查敌对实体；默认开启。
  - `threat_distance`：机器人或床周围的三维威胁距离，默认 8 格，最大 61 格。只能检查客户端已接收的实体，不能保证服务器一定允许入睡。
  - `timeout_seconds`：包括搜索、移动、确认和等待醒来的总时间预算。
  - `move_timeout_seconds`：每张床的寻路预算；服务端明确拒绝或寻路超时后可尝试下一张床。
  - `confirm_timeout_seconds`：点击床的 HTTP 等待上限、随后确认入睡的轮询预算，以及醒来后等待时间/天气数据同步的确认窗口；各窗口均受任务总预算限制。
  - `poll_interval_seconds`：状态轮询间隔，默认 0.5 秒。
  - `cancel_event`：可选 `threading.Event`；设置后中断等待并在请求边界退出。已经发出的同步 HTTP 请求仍受其请求超时限制；控制器可以同时调用 `client.stop()` 停止寻路。

* **前置条件与安全检查**：
  1. 状态必须为 `ready=True`、`alive=True`，维度必须是 `overworld` 或 `minecraft:overworld`；下界、末地和未知维度一律拒绝点击床。
  2. 必须已经在睡眠中，或 `is_day=False` / `thundering=True`。仅正常白天不会执行动作。
  3. 移动后和点击床前再次检查生命、就绪状态、维度和昼夜，避免把旧状态当成当前位置。

* **动作序列**：
  1. 使用 `/find_blocks` 的 `types` 列表查询全部 16 色床，取得带坐标的方块列表，按距离排序。当前睡眠技能不从开发 `MemoryEngine` 自动召回床位。
  2. 根据 `part`、`facing` 合并床头和床脚；任一半 `occupied=True` 则跳过该床。
  3. 检查机器人周围威胁，调用 `move_to(..., range=2)` 接近床。不可达时尝试下一张；HTTP 请求超时意味着动作状态未知，立即失败并请求停止，不自动重发动作。
  4. 复查距离、床方块和占用状态，再检查床及机器人附近威胁。遇到怪物返回失败，**不会声称已搭高或撤退**。
  5. 确认仍在主世界且床可交互后调用 `/interact`，随后轮询状态；单次 HTTP 成功或 `interacted=True` 不等于成功入睡。
  6. 满足以下任一证据链才返回成功：
     - 观察到 `is_sleeping=True`，随后观察到清晨/雷暴结束且 `is_sleeping=False`；
     - `/interact` 明确返回 `slept=True`，且前后状态从夜间/雷暴转为正常白天，用于单人服瞬时跳夜、来不及观察躺床状态的情况。
  7. 醒来事件和时间/天气更新可能分包到达。已观察到睡眠后，如果先看到 `is_sleeping=False` 但仍是夜间/雷暴，最多再等 `confirm_timeout_seconds` 确认时间和天气，期间继续检查生命、维度和取消；窗口截止仍未结束才返回 `sleep_interrupted`。若窗口内重新观察到睡眠，回到等待醒来状态，不再次点击床，也不延长任务总预算。单独观察到天亮、没有入睡证据，仍返回 `sleep_unverified`。

* **返回证据**：`SkillResult.data` 包含 `bed_pos`、`state`、`attempts`、`evidence` 和耗时；成功还包含 `verification` 与关键最终状态。启动时已经睡着可能没有已知床坐标，此时 `bed_pos=None`。
* **取消与失败恢复**：已开始动作的任务失败后尽力调用 `/stop`，清理失败会保留错误码。不会通过再次点击床来猜测起床：当前 API 的同一点击也能触发入睡，存在状态竞态；因此取消时机器人可能仍躺在床上，由服务器正常唤醒。`/stop` 本身的底层取消能力及 HTTP 延迟仍是执行边界。
* **主要错误码**：
  - `bad_args`：参数类型、范围或时间预算不合法。
  - `not_ready` / `dead` / `disconnected` / `unsafe_dimension`：机器人状态不适合睡眠。
  - `daytime`：正常白天，或到床前已天亮。
  - `no_bed_nearby` / `bed_occupied` / `bed_unavailable`：没有床、床被占用或目标已变化。
  - `path_unreachable`：候选床无法接近。
  - `monsters_nearby`：检测到邻近敌对实体，没有执行自动战斗或撤退。
  - `sleep_rejected` / `sleep_unverified` / `sleep_interrupted`：交互被拒绝、缺少睡眠证据、入睡后提前醒来。
  - `cancelled` / `timeout`：主动取消或任务总预算耗尽。
  - `request_timeout`：HTTP 等待超时，后台动作结果未知；不自动重试。其他 API 错误码保留，不转成空列表或成功。

---

### 5. `calculate_stronghold`（纯要塞计算）

- 实现：`skills/stronghold.py` 的 `CalculateStrongholdSkill`，可由 Python 或独立 CLI 调用。`agent/tools.py` 仅保存独立 `STRONGHOLD_TOOL_DEFINITION` 常量；starter/daily 模型工具目录与运行时没有注册此动作。
- 输入：`throws` 为 1～32 次独立主世界投眼观测，每项含 `x/z/bearing_deg/sigma_deg`；`top_k` 默认 5。角度采用原版 yaw 度数，南 0°、西 90°。
- 输出：`SkillResult.data` 含官方 Ninjabrain Bot 计算出的候选区块、投眼目标点、下挖参考点和条件概率；不代表已找到传送门房。
- 前置条件：已安装固定版本的计算 JAR 和 JDK 11+；观测必须来自同一世界、同一要塞，多点采样先归并为一条射线。
- 错误码：`invalid_observations`（无效或重复观测）、`calculation_failed`（依赖缺失、超时或无一致候选）。
- 无游戏动作，无需回滚。采样与计算分离；CLI `capture` 会投眼，细节见 [docs/STRONGHOLD.md](STRONGHOLD.md)。

---

### 6. `ChopTreeSkill`（独立整树伐木）

模块 `skills/mining.py`，构造函数接收 client 和可选 memory。实际调用：

```python
execute(target_count=5, max_duration_seconds=60.0, search_radius=32,
        clean_entire_tree=True, log_types=None)
```

目标数为原木数量，时间单位秒、半径单位格。它查找原木、聚类树干、逐块采集并返回数量/耗时等 `SkillResult.data`；是否取得物品仍应核查真实背包。未进入模型目录，默认聊天也未注入该对象；不能假定它继承 harness 的 pending、取消与安全门禁。不得与正在运行的生存动作并行调用。

### 7. `CraftItemSkill`（独立配方合成）

模块 `skills/crafting.py`，使用 `core/recipes.py` 的 `RecipeBook` 与 `data/recipes.json`，当前读取 886 个产物。实际调用：

```python
execute(item="wooden_pickaxe", count=1, auto_prerequisites=True,
        retrieve_table=True, max_duration_seconds=60.0)
```

支持查询 2×2/3×3 配方、前置材料解析、工作台放置与可选回收。返回 `SkillResult`，包含物品、数量、步骤和库存等数据；本轮只做目录读取及导入检查，没有实服制作验收，也不保证全部配方正确。模型 `craft_item` 仍由生存技能执行，不调用此类；`#craft` 需要显式注入对象，`main.py` 尚未提供该注入。

### 8. `NetherPortalSpeedrunSkill`（独立门户分步执行）

模块 `skills/portal_speedrun.py`，`CastNetherPortalSkill` 只是导出别名。调用者必须准备坐标、材料与独占客户端控制权：

| Python 方法 | 参数 | 范围 |
| --- | --- | --- |
| `build_scaffolding(x0,y0,z0)` | 门框基准坐标，单位格 | 放置辅助脚手架 |
| `execute(step=1, **kwargs)` | 阶段 1/2/3 和对应参数 | 分派单阶段，不自动规划整条流程 |
| `execute_step_1(start_sand_pos,stone_pos,water_pos,red_sand_pos)` | 四个 `[x,y,z]` | 放石/放水/移动 |
| `execute_step_2(stone_pos,base_positions,red_sand_pos,water_placement_pos=None)` | 方块与底部目标坐标 | 开底部位置并浇筑 |
| `execute_step_3(x0,y0,z0,temp_stand_pos=None)` | 基准和可选站位 | 后续浇筑、收水、点火和门方块读取 |

`PROCEDURAL_TOOL_DEFINITIONS` 的 `cast_nether_portal(axis)` 尚无与之匹配的完整 handler，不可直接注册给模型；当前没有自动搜池、选锚、任意朝向整流程或 harness 中断恢复契约。门户方块形成也不等于机器人已进入下界，更不等于服务器 advancement 已完成。

`PortalTelemetryRecorder.record_step(...)` 主动查询 API，可执行传入的 `action_func`；`dump_json()` 默认写 `logs/telemetry/`，未接入 EventMonitor。`LavaLakeGenerator.generate_random_lake(...)` 通过 RCON 生成地形，只用于专用靶场。三步实服测试默认跳过，随机矩阵是独立脚本；本轮未验证零伤害、零溢水或任何实服成功率。入口和门禁见 [DEBUG_GUIDE.md](DEBUG_GUIDE.md)。

### 9. `MineShaftSkill`、独立 `CaveExploreSkill` 与通用 3D 空间世界层

- **分层模块拆分**：
  - [`core/world_3d.py`](../core/world_3d.py)（`VoxelWorld3D`、`FrontierNode`、`OreNode`）：维护作用域隔离（可选 `runtime/world_3d/` chunk-zlib SQLite 持久化）的统一 3D 世界内存，记录体素映射、访问站位 `visited_stances`、清扫覆盖域 `scanned_cells`、未探前沿 `open_frontiers`、矿点注册表 `ore_registry`（`pending / mined / skipped_quota / skipped_tool / skipped_hazard / unreachable / seen`）、受阻有向边 `blocked_edges` 与脚手架坐标 `placed_scaffolds`。
  - [`core/pathfinder_3d.py`](../core/pathfinder_3d.py)（`Pathfinder3D`）：纯 3D 空间规划器，封装局部洪水扩图与前沿/矿点注册（`plan_horizon`）、基于 `VoxelWorld3D` 的有向 3D A* 全局跨洞跳点寻路（`find_path`：先 `walk(dy=0,+1,-1)`，后 `micro_dig`、`drop`/`scaffold_drop`、**锚定对岸实心地面或矿点 `bridge`** 与 `pillar`），以及 `find_ore_vantage_path`；所有扩张自动避开 `world.blocked_edges`。
  - [`skills/world_navigator.py`](../skills/world_navigator.py)（`WorldNavigator`、`ReversibleTrail`）：单步与路径执行器（统一经由 `stand_cell`/`dig_block`/`place_block`/`pillar_up`/`scaffold_drop` 执行），遇到未预料小阻碍/顶头方块可自主微掘清理并在失败时写入 `world.blocked_edges`；提供四级撤退恢复流水线 `retreat_to_entry`（直飞入口 → 缓存 3D A* 全图撤回 → `pillar_undo` 安全逆向面包屑 → `escape_to_surface` 回地面）。
  - [`skills/ore_harvester.py`](../skills/ore_harvester.py)（`OreHarvester`）：矿脉连通追踪（`mine_vein`，`collect=False` 配合实体与地面扫描的踩捡收集 `collect_mined_drop`）、配额/镐级/岩浆邻居过滤，以及 `harvest_reachable_at`；完成挖矿后回写体素缓存失效与 `OreNode` 状态。
  - [`skills/cave_explore.py`](../skills/cave_explore.py)（`CaveExploreSkill`，支持复用外部传入 `world: VoxelWorld3D` 或 `persistent_world=True`）与 [`skills/mining.py`](../skills/mining.py)（`MineShaftSkill`，通过 `ctx.extra["world_3d"]` 共享体素记忆并与 `CaveExploreSkill` 联动），共用纯逻辑探测/分类模块 `core/cave_probe.py`、`core/mining_targets.py` 与 `core/structure_spec.py` 常量。二者均未放入 `starter 9 / daily 31 / competition 36` 模型目录。
- **洞穴分类决策与探索策略**：
  - `unsafe`（矿道主支路遇险时封口并返回平台、`cave_abandoned` 上限 3 次）：任意岩浆/刷怪笼、水类方块 `>= 3`、空腔高度 `>= 10`、落差 `>= 4`；`min_enter_torches` 默认为 `0`（无火把仍允许进入自然洞穴采煤铁；背包有煤/木炭与木棍或木板时先自动合成火把，仅当显式配置 `min_enter_torches >= 1` 时才返回 `missing_torch`）。
  - `small_bubble`：空气 `<= 4` 且无水/岩浆/刷怪笼，最多连续补 1 格地板后继续直挖。
  - `enter`：`mineshaft`（木板/栅栏/铁轨合计 `>= 3`）或低矮安全洞（`height < 10` 且 `drop < 4`），默认按水平 `<= 48` 格、最多访问 `600` 格、超时 `360s`、**每 `24` 格插一支火把**（空间去重 `>= 18` 格）探索；废弃矿井仅在持有剑或剪刀时清理 `cobweb`，不碰 `chest_minecart`。
- **洞中防卡死、防兜圈、锚定搭桥与跨分支 3D 穷尽寻路**：
  - `lateral_air_clearance` 优先引导机器人在洞穴/通道中线行走，避开贴墙刮边。
  - `flood_safe_cave_graph`（[`core/cave_probe.py`](../core/cave_probe.py)）与 `Pathfinder3D` 均强制 `anchor_pos` / `anchor_radius` 硬边界过滤、`blocked_edges` 规避，以及**锚定跨涧搭桥约束**（`bridge` 桥长 `1..max_bridge_steps` 的落点前方/邻位必须存在自然实心地面或紧邻安全矿点，且前沿节点本身脚下必须为实心方块，杜绝朝悬崖虚空延伸凌空天桥）；在四邻列先完整评估所有自由步行高差（`dy in 0,+1,-1`），再评估 `micro_dig` / `drop` / 锚定 `bridge` / 受控 `pillar`。
  - [`explore_cave`](../skills/cave_explore.py) 通过 `VoxelWorld3D` 持久化累积全洞三维空间体素、未探前沿 `open_frontiers` 与 `ore_registry`：局部 `Pathfinder3D.plan_horizon`（≤560 节点）探尽当前支路后，自动查询 `VoxelWorld3D` 全局未收割矿脉与优先队列前沿并调用 `Pathfinder3D.find_path`（高达 2400 节点 3D A*）跨分支精确折返跳转，必要时再触发全局 horizon 扩图，直至局部前沿与记忆前沿穷尽、配额满或安全预算耗尽。
  - `WorldNavigator` 在单步受阻时于循环内完成微掘清障或记录 `blocked_edges` 触发 A* 绕路，并通过 `follow_path` 将连续直线平滑步行步合并为疾跑长步（最多 8 格），减少走停顿挫；同时通过 `retreat_to_entry` 四级恢复与 `escape_to_surface(..., max_ascend=160)` 保证可回入口或安全回地面。**保持游戏会话在线**：`MinecraftAgentClient.attach_or_login` 与 `main.py --keep-connected` / `mc_api.keep_session_on_exit` / `competition` 模式退出时不调用 `auth/logout`。
- **目标挖矿与实地踩捡边界**：所有 `dig` 调用均传 `collect=False`（步行踩捡）。主矿道每步复用缓存读取 8 个新暴露邻面；洞内与洪水图扫描覆盖 `oy in (-2..5)` 且同时校验眼距 `<= 4.5` 与高位矿脚底角距 `feet_corner_dist <= 3.95`（匹配 `mc-agent-api` 的 `goNear(3.5)` 零寻路阈值）；挖爆矿块后若未直接入包，`_collect_mined_drop` 会结合 `get_entities` 掉落物位置及矿点下方真实落脚层主动走进安全落点，并覆盖拐角绕行、1 格高矮缝拆顶防卡头、山崖高台攀登与 2 格竖井落底捡取后搭柱脱困（受阻后复核背包避免重复卡等）；按 `targets` 区分 `mined / skipped_quota / skipped_tool / skipped_hazard / unreachable / seen`；单条矿脉延伸 `<= 16` 格。
- **原地柱搭高与洪水安全子图**：
  - `skills/action_workflow.py` 提供 `pillar_up`（支持落沙/砂砾占格自清理与可选顶头允许列表）、`pillar_down`、`scaffold_drop`（分段垫脚无伤下落）与 `escape_to_surface`（`max_ascend=160`，覆盖深层板岩、`wall_torch`、原矿脉与防烟囱 `tolerance=0.35` 往返抖动，避液清顶原地搭柱逃回地表）。
  - `core/cave_probe.py` 提供 `flood_safe_cave_graph`、`reconstruct_flood_path` 与 `lateral_air_clearance`。
  - `CaveExploreSkill.discover_and_explore_caves`（结合岩石顶板检索与陡坡/熔岩/落差全图导航）可由 `MineShaftSkill.run_with_journal(..., prefer_caves_first=True)` 或 `mine_targets_from_surface` 联动调用。
- **主世界分层矿脉分布与地表下矿防卡死**：
  - `core/mining_targets.py` 新增 `ORE_SPAWN_Y_BANDS`、`target_spawns_at_y`、`unmet_spawnable_targets_at_y` 与 `recommended_mining_bands`（遵循 `coal` 仅 `Y=0..192` 下层不生成、`diamond` 在 `Y=-64..15`、`iron` 在 `Y=16` 峰值且全层可采等主世界分布）。
  - `MineShaftSkill.build_mine_shaft` 与 `mine_targets_from_surface` 支持从地表出发先探浅洞再沿单条连续主阶梯下穿分层目标（`target_ys=[28, -54, 16]`；若浅层独占矿物如 `coal` 已提前达标且仍有深层目标则自动跳过 `Y>=20` 鱼骨支线；阶梯遇暗河/洞穴支持最多 10 格水平跨步绕行并避开已有 `trail` 防止 U 型折返自困；`_retreat_downsampled` 异常收敛与 `deep_retreat_done` 护栏防止撤回顶端后重试再次下钻；`_ensure_returned_to_surface` 优先复走 `retreat_trail` 干阶梯，其次 3D A* 与 `240s` `critical_health=1.0` `escape_to_surface` 确保回到地表）。
- **回家/休整与检查点**：每推进 `4` 格分支及每次探索结束后评估 `home_thresholds`（空槽 `<= 3`、生命 `< 12`、饥饿 `< 10` 且无食物、火把 `< 4`、可放置方块 `< 16`、无可用镐或剩余耐久 `< 10%`；剩余时间 `< 30s` 安全收尾）。洞内优先自动进食回血（`DEFAULT_EDIBLE_FOODS`），仅在背包满、断镐或无食物且血量危急时早退。
- **深层鱼骨挖矿与检查点续挖**：`MineShaftSkill.build_mine_shaft` / `run_with_journal` 支持 `target_y`、`target_ys`、`mining_mode="single"|"fishbone"`、`rib_length=12`、`rib_spacing=4`、`max_trunk_steps=40`。
- **伤害容忍与战斗限制**：`MineShaftSkill` 与 `CaveExploreSkill` 默认 `allow_damage=True`、`critical_health=6.0`，并在移动/放置动作上向 `mc-agent-api` 传递 `stop_on=["death"]`，不再因 `1~2` 点擦伤/摔伤立即中断动作或退洞；仍不主动迎战或清理怪物，近战攻击与防御留待后续单独模块补齐。

---

### 10. `ContainerSkill`（独立容器存取与熔炉烧制）

模块 `skills/container.py`，封装对双箱/单箱/木桶与熔炉的确定性窗口操作（基于 `mc-agent-api` 的 `window_view` 与 `window_act`，单次批操作上限 `8` 项、自动拆批），未加入 `starter 9 / daily 31 / competition 36` 模型目录：

```python
execute(action="inspect|deposit|withdraw|smelt", pos=[x, y, z], ...)
inspect(pos)
deposit(pos, items, *, count=None, section="container", ensure_all=True)
withdraw(pos, items, *, count=None, section=None, ensure_all=True)
smelt(pos, input_item, count=1, *, fuel_item=None, wait_complete=True, collect_output=True)
```

- **存取特性**：支持 `str` 或 `{item: count}` 映射，支持大箱子 `54` 槽 `"container"` 段、空箱满箱 `partial` 部分成功回报，以及熔炉默认按 `"output"` 段取出；要求箱子上方无实心遮挡方块（如工作台顶部放熔炉、箱子上方保留空气）。
- **熔炉自动烧制**：基于 `FUEL_BURN_ITEMS`（煤炭/木炭按 `8` 格、木板/原木 `1.5`、木棍 `0.5`）自动选配最小必需燃料；开炉前先清理既有 `"output"` 成品，支持 `count > 16` 多组轮询、`input==0 and output==0` 早退及 `iron_ingot` 等产物的严格确认。

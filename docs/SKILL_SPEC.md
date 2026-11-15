# BotSleep 技能规范文档 (SKILL SPECIFICATION)

本规范定义了 BotSleep 内部所有**程序化宏技能 (Procedural Skills)** 的输入输出契约、前置条件、超时限制与故障降级逻辑。

---

## 一、技能基础契约 (BaseSkill Contract)

单一宏技能采用 `skills/__init__.py` 的 `BaseSkill` 契约，客户端由构造函数 `BaseSkill(client)` 注入，生命周期钩子为：

- `validate_preconditions(**kwargs) -> bool`：校验动作前置条件。
- `execute(**kwargs) -> SkillResult`：核心动作逻辑。
- `rollback(reason: str) -> None`：异常中断时的恢复钩子，基类默认不执行动作；具体恢复以各技能实现为准。

`SkillResult` 包含 `success`、`message`、`data`、`error_code`。生存基础动作通过 `SurvivalSkills.execute(name, args, cancel_event=None, *, recovery_context=None)` 分派；`recovery_context` 由 harness 内部提供，不是模型参数。当前运行时为 `main.py` 启动的 `core/survival_harness.py`，不使用 `agent/loop.py` 旧骨架。starter/daily 分别公开9/31项模型工具，仅接受已注册动作；实际契约见 [SURVIVAL.md](SURVIVAL.md) 与 [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md)。

---

日常生产技能及验证边界见 [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md)。下面的 `pillar_up`、`mlg_water` 及其 `telemetry.ReflexDaemon` 触发机制是未接入的设计规范，不属于可执行模型工具。

## 二、核心技能规范

### 1. `continuous_radar` (常态化雷达自动勘探技能)

* **功能**：在 Bot 移动与探索过程中，常态化对周边空间发射 3D 方块雷达，抓取床、箱子、工作台、矿物、水源、岩浆等战略 POI，并自动更新至 `MemoryEngine` 空间记忆库。
* **特性**：**可开关的常态自动化技能 (Toggleable Background Autonomous Skill)**。
* **参数与控制**：
  - `enable()` / `disable()` / `toggle()`：开关控制。
  - `min_move_distance: float = 6.0`：位移触发阈值（当位移超过 6 格自动触发一次全景扫荡）。
  - `scan_radius: int = 24`：雷达扫描半径（最大 32 格）。
* **产出物**：
  - 自动更新 [data/game_logic/landmarks.json](file:///E:/Program%20Save/VisualCode/BotSleep/data/game_logic/landmarks.json)（POI 数据库）。
  - 自动记录航迹与探索区块至 [data/game_logic/spatial_grid.json](file:///E:/Program%20Save/VisualCode/BotSleep/data/game_logic/spatial_grid.json)。
  - 生成 `episodic_memory.jsonl` 情景记忆。

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
  1. 使用 `/find_blocks` 的 `types` 列表查询全部 16 色床，取得带坐标的方块列表，按距离排序。若空间记忆库 `MemoryEngine` 已探明床位坐标，可优先召回。
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

### 6. `chop_tree` (智能伐木与清理整树技能)

* **功能**：定位最近的可达树木，自底向上完整采伐整根树干，支持工作时长限制与目标数量阈值，返回精准完成状态。
* **模块归属**：`skills/mining.py` (`ChopTreeSkill`)。
* **参数**：
  - `target_count: int = 5`：目标收集的原木数量（达到后停止）。
  - `max_duration_seconds: float = 60.0`：单次工作最大超时时间（秒）。
  - `search_radius: int = 32`：搜树半径。
  - `clean_entire_tree: bool = True`：是否完整采伐整棵树（防止空中残留浮空树枝）。
  - `log_types: Optional[List[str]] = None`：指定采伐的原木类型（默认自动适配橡木、白桦、云杉等全原木）。
* **核心动作流程**：
  1. 通过雷达/`find_blocks` 探测周边所有原木方块，按水平聚类算法区分独立树干并按距离排序。
  2. 自动剔除因深坑/阻隔导致不可达的树木（`unreachable_trees` 故障自动重试转移）。
  3. 自底向上逐块采掘并自动拾取掉落物。
  4. 达到目标数量或超时后，返回 `SkillResult`，写入 `episodic_memory.jsonl` 情景记忆。
* **输出数据 (Result Data)**：
  - `harvested_count`: 采集的原木总数。
  - `target_count`: 目标数。
  - `elapsed_seconds`: 实际耗时。
  - `target_reached`: 是否达成目标。
  - `time_limit_reached`: 是否超时截断。
  - `trees_cleared`: 彻底砍光的完整树木数量。
  - `breakdown`: 各类原木采掘明细（如 `{"oak_log": 6}`）。

---

### 7. `craft_item` (智能合成与工具制作技能)

* **功能**：自动区分 2x2（背包直合）与 3x3（工作台合成）配方，支持前置材料递归逆向分解合成（如原木 -> 木板 -> 木棍 -> 木镐），自动寻找/放置工作台，并在合成完成后自动将工作台挖掘回收回背包。
* **模块归属**：`skills/crafting.py` (`CraftItemSkill`)，底层配合 `core/recipes.py` (`RecipeBook`) 与 `data/recipes.json`（内置 Minecraft 1.21.11 完整 886 种配方库）。
* **参数**：
  - `item: str`：目标合成物品名（如 `wooden_pickaxe`, `stick`, `crafting_table`, `oak_planks`）。
  - `count: int = 1`：目标合成数量。
  - `auto_prerequisites: bool = True`：是否自动从基础原料层级推导并合成中间产物（例如只有原木时，自动合成木板与木棍）。
  - `retrieve_table: bool = True`：3x3 合成完成后是否自动回收工作台（让工作台留在背包备用，不遗失在野外）。
  - `max_duration_seconds: float = 60.0`：合成动作超时时间。
* **执行工作流 (Pipeline)**：
  1. **配方分类**：检查配方尺寸。若 $\le 2\times 2$（如木板、木棍、工作台、火把），直接在玩家背包制作。
  2. **3x3 工作台调度**：
     - 若周围 12 格内已有放置好的工作台，直接寻路复用。
     - 若无工作台且背包中无工作台，使用 2x2 自动用 4 块木板合成工作台。
     - 若当前脚下为树叶/水域等不稳固方块，自动微移至坚实地面（`_ensure_solid_ground`）。
     - 调用 `place_block("crafting_table")` 放置于身侧，并更新 `MemoryEngine` 空间地标。
  3. **前置原料预算与批处理合成**：
     - 精确计算所需全部中间材料（如木镐需要 3 木板 + 2 木棍，1 次木棍合成消耗 2 木板产生 4 木棍，总计需 9 块木板，推导出需 3 根原木）。
     - 自动合并批量合成（一次性将 3 根原木合成为 12 块木板），极大提升合成速度与效率。
  4. **3x3 界面交互合成**：
     - 与工作台交互，自动填充配方并取回目标物品。
  5. **工作台自动回收**：
     - 若 `retrieve_table=True`，自动对放置的工作台执行 `dig(collect=True)`，将其拾回背包。
* **输出数据 (Result Data)**：
  - `item`: 目标物品。
  - `requested_count`: 目标数量。
  - `total_in_inventory`: 背包中该物品现存总量。
  - `gained`: 本次净增数量。
  - `duration_seconds`: 耗时。
  - `steps`: 详细执行步骤链。
  - `final_inventory`: 最终背包全量快照。

---

### 8. `cast_nether_portal` (速通级地狱门水桶浇筑技能)

* **功能**：自动检索并评估周边岩浆池空间与源头储量，通过水桶倒模机制浇筑出完整标准的 10 块黑曜石地狱门框架，并使用打火石激活，最后全面验收框架与传送门方块的具体状态。
* **模块归属**：`skills/portal_speedrun.py` (`NetherPortalSpeedrunSkill`，兼容别名 `CastNetherPortalSkill`)。
* **参数**：
  - `anchor: Optional[List[int]] = None`：地狱门左下角基准锚点坐标（若不传则基于岩浆池边缘自动选点）。
  - `axis: str = "x"`：地狱门朝向（`"x"` 沿 X 轴东西向，`"z"` 沿 Z 轴南北向）。
  - `search_radius: int = 32`：搜寻合格岩浆池的最大半径。
* **前置条件与空间校验**：
  1. 背包中备有 `water_bucket`（或空桶）、`flint_and_steel`、任意镐子与 $\ge 4$ 个耐火固体方块。
  2. **足够空间的岩浆池判定**：
     - 岩浆池内具有 $\ge 10$ 格连续或近邻的静态岩浆源（`level == 0`）。
     - 建造区上空必须拥有 $\ge 5$ 格的净空垂直高度（不可处于密闭压顶低矮矿洞）。
     - 岸边具有可供 Bot 安全站位与作业的干燥实心方块地面。
* **执行步骤**：
  1. 扫描岩浆池并评估空间与岩浆储量；选定安全平整锚点。
  2. 计算地狱门框架 10 块黑曜石目标坐标（底座 2 块、左侧柱 3 块、右侧柱 3 块、顶梁 2 块）。
  3. 依次执行舀岩浆、水流冷却成型与回收水源的标准循环（避免大范围溢水破坏主岩浆湖）。
  4. 使用打火石右键基座黑曜石点火激活。
  5. 完整回扫框架 10 块黑曜石及内圈 6 格空间状态（`nether_portal` 判定）。
* **产出物 (Result Data)**：
  - `obsidian_count`: 经验收确认的黑曜石块数（必须为 10/10）。
  - `frame_complete`: 门框是否 100% 完整。
  - `portal_active`: 紫色地狱门方块是否成功生成。
  - `inspection`: 包含框架和内圈全部 16 个具体方块名称与元数据的详细快照字典。

---

## 三、面向 harness AI 的策略 Skill

`skills/stronghold_localization/SKILL.md` 是供游戏 AI 按需读取的投眼、选点和导航策略，详细推导与来源在其 `references/research.md`。它与 Python `BaseSkill` 动作执行器用途不同。

本策略尚未自动加载到模型上下文，统计误差估计与自动导航优化也未接入运行时代码；不要将文档存在等同于功能已实现。

策略 skill 默认面向小模型：短正文、真实接口调用表、最多五步流程，原理和长示例放入 `references/`。未绑定入口须明确标注；会话、采样、计算和并发监控由 harness 负责。完整编写约定见 [AGENTS.md](../AGENTS.md)，未来赛事入口见 [HARNESS_INTERFACES.md](HARNESS_INTERFACES.md)。

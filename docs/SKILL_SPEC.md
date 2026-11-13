# BotSleep 技能规范文档 (SKILL SPECIFICATION)

本规范定义了 BotSleep 内部所有**程序化宏技能 (Procedural Skills)** 的输入输出契约、前置条件、超时限制与故障降级逻辑。

---

## 一、技能基础契约 (BaseSkill Contract)

所有宏技能均继承 `BaseSkill`，提供标准的生命周期钩子：
- `validate_preconditions(context) -> bool`：校验背包资源、空间高度、网络状态。
- `execute(context, **params) -> SkillResult`：核心动作逻辑。
- `rollback(context, reason) -> None`：动作异常中断时的安全恢复（如清空跳跃、恢复正常视线）。

---

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

* **功能**：在夜间或雷暴天气下，主动探测附近安全床位，寻路至床边并右键入睡，清晨自动唤醒。
* **参数**：
  - `radius: int = 16`：床位搜索半径。
  - `threat_check: bool = True`：是否先校验并避开周边敌对生物。
* **记忆联动**：若雷达已探明床位坐标，直接从 `MemoryEngine` 调取最近床位寻路，无需从头漫游盲扫。

---

### 5. `chop_tree` (智能伐木与清理整树技能)

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

### 6. `craft_item` (智能合成与工具制作技能)

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

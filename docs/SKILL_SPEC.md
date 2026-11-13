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

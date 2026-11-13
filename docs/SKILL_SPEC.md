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

### 1. `pillar_up` (自动垂直搭高)

* **功能**：在当前位置向上跳跃并在脚下放置实心方块，使角色垂直升高 $N$ 格。
* **参数**：
  - `height: int = 3`：目标搭高层数（建议 1~10）。
  - `block_filter: Optional[List[str]] = None`：允许消耗的方块类型列表（默认：圆石、深板岩、泥土、橡木木板等实心方块）。
* **前置条件**：
  1. 背包中实心方块总数 $\ge height$。
  2. 头顶垂直 $height + 2$ 格空间内无阻碍（非实心方块）。
* **动作序列**：
  1. 视线强制旋转至正下方：`yaw = current_yaw, pitch = 1.57 (90°向下)`。
  2. 选定背包内优先级最高的方块并装备至主手。
  3. 循环 $i = 1 \dots height$：
     - 发送跳跃姿态包：`posture(jump=True)`。
     - 等待角色垂直高度 $y$ 升至当前方块顶面以上（约 250ms）。
     - 向脚下原方块顶部面发送 `/place` 包。
     - 判定放置成功且 $y$ 坐标升高 1 格。
  4. 恢复站立姿态，解除视线锁定。
* **错误码**：
  - `insufficient_blocks`：方块储备不足。
  - `overhead_blocked`：头顶空间受阻。
  - `placement_failed`：方块未被服务器认可或卡在生物碰撞箱。

---

### 2. `mlg_water` (自动落地水 / 高空坠落缓冲)

* **功能**：角色在高空坠落时，在着地前最后一刻向地面倒水抵消跌落伤害，并在着地后立即收回水源。
* **触发机制**：由 `telemetry.ReflexDaemon` 线程以 20Hz 频率实时守候。
* **激活条件**：
  1. 角色处于无支撑下落状态 (`in_air = True`, `velocity_y < -12`)。
  2. 预估着地距离处于危险高度（距下方面积 $< 3$ 格）。
  3. 背包或手持栏存在 `water_bucket`。
* **动作序列**：
  1. 抢占动作锁，立即中止当前非紧急动作（如长距离寻路）。
  2. 主手切换至 `water_bucket`。
  3. 视线快速下切至预测落地点。
  4. 在接触地面的一瞬间（距地面约 1~1.5 格）向地面发送 `/interact` 倒水。
  5. 角色接触水源判定（`in_water = True` 或伤害免疫生效）。
  6. 延迟 80~150ms 再次发送 `/interact` 将水收回为空桶。
* **错误码**：
  - `no_water_bucket`：身上没有水桶。
  - `timing_missed`：未能及时放置水源。

---

### 3. `auto_sleep` (智能找床睡眠)

* **功能**：在夜间或雷暴天气下，主动探测附近安全床位，寻路至床边并右键入睡，清晨自动唤醒。
* **参数**：
  - `radius: int = 16`：床位搜索半径。
  - `threat_check: bool = True`：是否先校验并避开周边敌对生物。
* **前置条件**：
  1. 游戏处于可入睡状态（`is_day == False` 或 `thundering == True`）。
* **动作序列**：
  1. 扫描周边半径内的 `*_bed` 方块。
  2. 若有怪物且 `threat_check == True`，优先评估威胁距离；若怪物处于 5 格内，先执行搭高避险或战术撤退。
  3. 寻路接近目标床方块（保持在 3 格交互距离内）。
  4. 发送 `/interact` 点击床铺。
  5. 状态轮询：等待 `is_sleeping == True`。
  6. 监听清晨事件（`is_day == True`）或点击床铺唤醒。
* **错误码**：
  - `no_bed_nearby`：搜索范围内未发现可用床位。
  - `path_unreachable`：床位位于不可达区域或封闭房间。
  - `monsters_nearby`：因周边怪物过近被服务器拒绝入睡。


### 4. `calculate_stronghold`（纯要塞计算）

- 实现：`skills/stronghold.py` 的 `CalculateStrongholdSkill`。
- 输入：`throws` 为 1～32 次独立主世界投眼观测，每项含 `x/z/bearing_deg/sigma_deg`；`top_k` 默认 5。角度采用原版 yaw 度数，南 0°、西 90°。
- 输出：`SkillResult.data` 含官方 Ninjabrain Bot 计算出的候选区块、投眼目标点、下挖参考点和条件概率；不代表已找到传送门房。
- 前置条件：已安装固定版本的计算 JAR 和 JDK 11+；观测必须来自同一世界、同一要塞，多点采样先归并为一条射线。
- 错误码：`invalid_observations`（无效或重复观测）、`calculation_failed`（依赖缺失、超时或无一致候选）。
- 无游戏动作，无需回滚。采样与计算分离；CLI `capture` 会投眼，细节见 `docs/STRONGHOLD.md`。


## 三、面向 harness AI 的策略 Skill

`skills/stronghold_localization/SKILL.md` 是供游戏 AI 按需读取的投眼、选点和导航策略，详细推导与来源在其 `references/research.md`。它与 Python `BaseSkill` 动作执行器用途不同。

本策略尚未自动加载到模型上下文，统计误差估计与自动导航优化也未接入运行时代码；不要将文档存在等同于功能已实现。

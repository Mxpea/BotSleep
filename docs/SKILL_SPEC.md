# BotSleep 技能规范文档 (SKILL SPECIFICATION)

本规范定义了 BotSleep 内部所有**程序化宏技能 (Procedural Skills)** 的输入输出契约、前置条件、超时限制与故障降级逻辑。

---

## 一、技能基础契约 (BaseSkill Contract)

单一宏技能采用 `BaseSkill` 契约，提供以下生命周期钩子。生存基础动作通过 `SurvivalSkills.execute(name, args, cancel_event)` 注册分派，同样返回 `SkillResult`，实际契约见 [SURVIVAL.md](SURVIVAL.md)，不会注册未实现的搭高/落地水骨架：
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
  1. 使用 `/find_blocks` 的 `types` 列表查询全部 16 色床，取得带坐标的方块列表，按距离排序。
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

# 调试与验证

## 当前游戏内命令

调试通过配置 `debug.enable_in_game_commands: true` 或命令行 `--debug` 开启。必须在 `debug.allowed_usernames` 中列出允许控制 Bot 的玩家，空列表不接受聊天指令。

支持 API `messagestr` 中的标准 `<Player> #command` 格式。不执行匿名系统消息、机器人自己的消息或未列入名单的玩家指令。服务器插件自定义的聊天格式尚未适配。

| 指令 | 实际行为 |
| 指令 | 参数 | 说明 | 示例 |
|---|---|---|---|
| `#help` | 无 | 列出当前 Bot 支持的全部调试指令 | `#help` |
| `#status` | 无 | 汇报 Bot 坐标、血量、睡眠状态及雷达状态 | `#status` |
| `#stop` | 无 | 请求中断当前动作并暂停自动任务 | `#stop` |
| `#sleep` | 无 | 恢复自动睡眠并请求一次尝试 | `#sleep` |
| `#resume` | 无 | 仅生存模式，解除暂停并重新观察 | `#resume` |
| `#radar` | `[on\|off\|scan\|poi]` | 3D 方块雷达控制与空间 POI 检索 | `#radar on` |
| `#chop` | `[目标数量=5] [超时秒=60]` | 寻找最近树木并自动连续伐木整树 | `#chop 5 60` |
| `#craft` | `<物品名> [数量=1]` | 智能合成物品（2x2直合，3x3自动放取工作台） | `#craft wooden_pickaxe 1` |
| `#inv` | 无 | 查看当前背包物品快照与数量 | `#inv` |

技能在后台线程执行，等待天亮不会阻塞聊天轮询。正在等待的同步 HTTP 调用受自身超时限制；停止回执只说明已经请求停止。取消睡眠不会再次点击床，以免起床/入睡竞态。Ctrl+C 会取消任务、等待工作线程结束，然后登出；清理失败不会跳过登出步骤。

## 日志与飞行记录仪

`debug.flight_recorder.output_dir` 相对配置文件目录。启用后写入 JSONL，时间为带毫秒的 UTC。所有决策与动作执行日志均以 JSON Lines 格式输出至 `logs/flight_YYYYMMDD.jsonl`，包含毫秒级时间戳、上下文与执行结果。遇到异常时，飞行记录仪会自动记录**现场快照**，方便开发者快速分析根本原因。

当前事件包括启动、睡眠开始、结果、停止请求、崩溃和退出。睡眠结果包含标准 `SkillResult`、尝试床位、错误码和验证证据。

生存模式还记录 `DECISION`、`STEP_RESULT`、`SAFETY_INTERRUPT`、`DEATH_OBSERVED`、`RECOVERY_REQUIRED`、`PAUSED` 和预算耗尽事件。无论飞行日志是否开启，`survival.state_dir` 都保存按服务器/机器人/world_id 隔离的原子检查点和事件记录。HTTP 超时或内部验证异常后的 pending 不会自动重放。

目前没有全量三维现场快照、模型推理追踪或自动回放。

## 离线回归测试

```powershell
python -B -m unittest discover -s tests -v
```

HTTP 集成用本机临时假服务验证真实请求和主入口生命周期，不连接 Minecraft。

## RCON 开发工具与靶场演练

`core/rcon.py`、`debug/playground.py` 为可选开发工具。普通自动睡眠不导入或使用 mcrcon，仅使用靶场时安装 `requirements-dev.txt`。

靶场会给物品、传送、放床、改时间或生成生物，只能在专用测试世界调用。实际游戏测试步骤和结果见 [TESTING.md](TESTING.md)。

### 常用靶场测试场景

#### 场景 1：自动落地水测试 (MLG Water Clutch Test)
- **步骤**：
  1. RCON 给 Bot 发放水桶：`/give SleepBot water_bucket 1`
  2. RCON 将 Bot 传送至 25 格高空悬空：`/tp SleepBot ~ ~25 ~`
  3. 观察 Bot 下落时是否在接地前 1~2 格触发水桶倒水并在着地后收回。

#### 场景 2：黑夜寻床入睡测试 (Night Sleep Test)
- **步骤**：
  1. RCON 清空周边生物并放置一张红色床：`/setblock ~3 ~ ~ red_bed`
  2. RCON 将时间强制切换至午夜：`/time set midnight`
  3. 观察 BotSleep 是否主动识别并入睡。

#### 场景 3：整树连续伐木测试 (Chop Tree Test)
- **步骤**：
  1. 玩家在游戏内输入 `#chop 5 60`。
  2. 观察 BotSleep 自动探测最近可达树干，自底向上挖掘，规避水域阻碍，并在砍伐完毕后自动拾取全部掉落物。

#### 场景 4：智能逆向合成木镐测试 (Wooden Pickaxe Crafting Test)
- **步骤**：
  1. 玩家或测试脚本清空背包并给予 8 根原木：`give SleepBot oak_log 8`。
  2. 在聊天框输入 `#craft wooden_pickaxe 1`（或运行 `tests/test_crafting.py`）。
  3. 观察 Bot 依次自动完成：
     - 背包 2x2 批量合成 12 块橡木木板
     - 背包 2x2 合成 1 张工作台
     - 背包 2x2 合成 4 根木棍
     - 寻找身旁坚实平地放置工作台
     - 使用工作台 3x3 界面精准合成木镐
     - 自动挖掘收回工作台至背包
  4. 输入 `#inv` 确认最终背包包含 `wooden_pickaxe`、`crafting_table`、`stick` 与剩余木板。

# BotSleep 人机协同调试指南 (DEBUG GUIDE)

为了让人类开发者和 AI 助手（如 Antigravity）能够零阻碍、高透明度地观察游戏世界、单步执行动作并排查偶发故障，BotSleep 设立了专门的**“三维一体调试体系”**。

---

## 一、游戏内聊天调试控制台 (In-Game Chat Debugger)

玩家在 HMCL 客户端中登录游戏后，可以直接在游戏聊天框输入以 `#` 为前缀的指令与 Bot 直接交互，无需切换到终端控制台。

### 支持的聊天指令规范

| 指令 | 参数 | 说明 | 示例 |
|---|---|---|---|
| `#help` | 无 | 列出当前 Bot 支持的全部调试指令 | `#help` |
| `#status` | 无 | 汇报 Bot 坐标、血量、饥饿度、雷达开启状态 | `#status` |
| `#stop` | 无 | 强制中断 Bot 当前所有动作并关闭常态巡航雷达 | `#stop` |
| `#radar on` | 无 | **开启常态化雷达勘探技能**（移动时自动扫描并入库） | `#radar on` |
| `#radar off` | 无 | **关闭常态化雷达勘探技能** | `#radar off` |
| `#radar scan` | 无 | **强制触发一次单次全景雷达测绘**并刷新空间知识库 | `#radar scan` |
| `#radar poi` | 无 | **查询当前记忆库中最近的 4 个关键 POI** 及距离 | `#radar poi` |
| `#pillar` | `[高度=3]` | 强制执行程序化自动搭高 | `#pillar 5` |
| `#sleep` | 无 | 强制触发寻找床位并入睡状态机 | `#sleep` |
| `#llm` | `<自然语言提示>` | 强制将用户输入交给大模型规划并执行下一步行为 | `#llm 帮我建造一个 3x3 的火把防护圈` |

---

## 二、RCON 上帝视角测试靶场 (Playground Harness)

通过直连服务端的 RCON 服务（`127.0.0.1:25575`），调试脚本可以瞬间构建复杂的边界测试场景，自动化验收宏技能。

### 常用测试场景 (Scenarios)

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

---

## 三、飞行记录仪与日志排查 (Flight Recorder)

所有决策与动作执行日志均以 JSON Lines 格式输出至 `logs/flight_YYYYMMDD.jsonl`，包含毫秒级时间戳、上下文与执行结果。遇到异常时，飞行记录仪会自动记录**现场快照**，方便开发者快速分析根本原因。

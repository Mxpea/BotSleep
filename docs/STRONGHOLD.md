# Ninjabrain Bot 计算集成

本模块以无界面 Java 子进程调用官方 **Ninjabrain Bot 1.5.2** 的计算核心，保留贝叶斯推断、角度误差、要塞生成环、生物群系偏移模型和最近要塞约束。没有启动其桌面程序，也不需要 F3+C、准星、剪贴板或热键。适用原版 Java 1.19+（包括本项目 1.21.11）的主世界生成规则。

## 安装与离线计算

需要 Python 3.11 和 JDK 11+（`java`、`javac` 在 PATH 或 JAVA_HOME 下）。纯计算仅用 Python 标准库；API 采样通过现有客户端，需要项目环境的 `requests`、`mcrcon`。

```powershell
.\.venv\Scripts\python.exe -m integrations.ninjabrain setup
.\.venv\Scripts\python.exe -m integrations.ninjabrain calculate --input data/examples/stronghold_throws.json --output logs/stronghold_prediction.json
```

`setup` 下载固定官方 JAR 并检查 SHA-256；后续计算完全离线，不会自动下载。JAR 和编译产物放在 `.cache/ninjabrain/`，不提交。示例是指向 `[128, 0]` 区块的合成数据，不能用来定位你的世界。

输入为 `{"throws": [{"x": 0, "z": 0, "bearing_deg": -90, "sigma_deg": 0.1}, ...]}`，也可直接读取下述采样会话文件。

- `x/z` 是同一条飞行轨迹上的射线起点，单位为主世界方块。
- `bearing_deg` 使用原版 yaw：南（+Z）0°、西（−X）90°、北（−Z）±180°、东（+X）−90°。不要直接传 mineflayer 的弧度 yaw。
- `sigma_deg` 是角度标准差，支持 0.001～5°；坐标能读到小数不代表方向零误差。
- 每次投眼只贡献一条观测。同一只眼的多个坐标用来拟合一条射线，不能当成多次独立投掷。

输出 `candidates` 按概率排序，包含 `chunk`、`eye_target`、`dig`、`distance_blocks` 和 `probability`。1.19+ 的计算目标位于区块 `(0,0)` 偏移；`dig` 是上游建议的 `(4,4)` 下挖参考点，**不是传送门房的精确坐标**。只输出前若干候选，因此所列概率不一定合计为 1。`--no-advanced` 可关闭上游最近要塞约束，默认开启。

## 从本地 API 采样（会消耗一只末影之眼）

等准备实机测试时再运行。确认机器人静止、主世界、背包有 `ender_eye`，并留意登录会替换同名旧会话；采样结束会登出。不会自动移动、挖掘、传送或使用 RCON。

```powershell
.\.venv\Scripts\python.exe -m integrations.ninjabrain capture --api-config G:\ww\mc-agent-api\config.json --world local-test --session logs/eyes-local-test.json --output logs/stronghold_prediction.json
```

也可用 `MC_AGENT_USER`、`MC_AGENT_PASSWORD` 配置凭据，或指定 `--username`；口令和 token 不写入会话。`--base` 可更换接口地址。`--world` 是你给存档起的标签，换世界必须换标签和会话文件。

第一次投眼后只能得到方向和概率分布。通过正常游戏方式横向移动一段距离，稳定站好，再以同一个 `--world/--session` 运行第二次。至少两次有横向基线的观测才有定位价值；最好先横移约 100～300 方块，视地形和距离调整。不要为获得更大基线越过不同要塞的最近区域。

采样过程：

1. 读取状态、背包和已有眼实体 ID，再并发调用 `/use_item` 与 `/get_entities`。
2. 只接受新出现、身份匹配的同一只眼，约每 100ms 记录位置，共 3.5 秒。
3. 用多点拟合水平飞行射线，要求至少 3 个不同水平位置、跨度至少 3 方块、轨迹无明显转弯或倒退，且玩家位置基本不变。
4. 保存原始坐标、实体 ID、拟合质量和投眼响应，随后调用计算核心。失败尝试也保存到 `attempts`，不会混入有效观测。

本地 API 的 `use_item` 源码会把最近的新实体当作眼，`landed_at` 实为当时坐标，`bearing_deg` 还取整。因此本模块不直接相信这些字段，而是按实体名字和 ID 跟踪飞行轨迹。实体不可见、多只新眼混入、跨度不足或 API 调用失败时会明确报错；此时眼可能已消耗。

默认角度误差来自 0.1 方块坐标舍入和轨迹跨度的保守估计，**尚未经真实投眼校准**；只有确认测量误差后才使用 `--sigma` 覆盖。模型概率不是实机成功率保证。非原版要塞生成、空岛未生成要塞、不同投掷指向不同要塞时，推断可能失败或失真。

## Python 与技能接口

```python
from integrations.ninjabrain import EyeThrow, StrongholdCalculator

result = StrongholdCalculator().calculate([
    EyeThrow(x=0, z=0, bearing_deg=-90, sigma_deg=0.1),
    EyeThrow(x=0, z=200, bearing_deg=-95.58, sigma_deg=0.1),
])
```

`skills.stronghold.CalculateStrongholdSkill().execute(throws=[...])` 返回标准 `SkillResult`；它只计算、不投眼。`agent/tools.py` 已登记 `calculate_stronghold` 工具结构，但现有 AgentLoop 分发仍是 TODO，不能据此认定模型已经能自主调用。

## 验证与来源

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖坐标方向、负区块、多点拟合、歧义实体、错误响应、缺失依赖，以及官方真实计算引擎的已知目标、基线变化和矛盾观测。未安装 JAR 时会显式跳过引擎测试。已完成五次真实投眼的坐标采样与多观测计算验证，结果见下节；传送门房尚未通过实地挖掘确认。

上游版本、许可证及分发说明见 `third_party/ninjabrain/NOTICE.md` 和 `LICENSE`。


## 2026-11-13 实机记录

本地 1.21.11 服务中，使用 `EyeObservationCollector` 采集了 5 次真实 `eye_of_ender` 轨迹，每次 11～13 个不同水平位置，跨度约 12.1 方块。使用默认未校准误差（约 0.61～0.74°），首选始终为区块 `(-86, -74)`，概率依次约为 0.72%、53.20%、73.27%、88.21%、98.42%。

- 投眼目标：`x=-1376, z=-1184`；下挖参考：`x=-1372, z=-1180`。不是传送门房精确坐标，也没有测出地下 Y。
- 原始会话：`logs/eyes-local-test-2026-11-13.json`；过程报告：`logs/stronghold_live_result.json`；最终结果：`logs/stronghold_live_prediction.json`。上述文件被 Git 忽略，换世界不能复用。
- 本轮消耗 5 只眼，64 → 59；最终生命值 20，停在 `(-1256.4, 70, -1427.3)`，保持在线。
- 移动过程中，寻路在河中超时并出现一次溺水伤害（降至 18），随后正常上浮、游泳上岸并恢复满血。这是移动与水中恢复的问题，不能将投眼成功等同于导航可靠。没有使用 RCON、管理员定位或读取存档，也没有挖掘。

离线复算真实记录：

```powershell
.\.venv\Scripts\python.exe -m integrations.ninjabrain calculate --input logs/eyes-local-test-2026-11-13.json
```

98.42% 是给定误差和原版生成假设下的模型概率，仍需实地确认；真实投眼样本验证了数据通路，但不等于已经完成误差标定。

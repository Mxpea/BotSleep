# 研究依据与实现差距

此文件供 harness 在需要解释误差、选择下一投眼位置或实现策略时提供给游戏 AI。`SKILL.md` 是运行策略；本文件是证据与推导，不代表功能已实现。

## 1. 原误差为什么偏宽

本地 API `src/session.js` 的 `fmt` 使用 `toFixed(1)`，所以 q=0.1 方块；单点舍入约 ±0.05 方块。`use_item(ender_eye)` 的方位角又经过整数取整，但坐标采样无需使用该整数角度。

当前 `integrations/ninjabrain/observations.py` 使用 `atan((0.1+最大残差)/跨度)` 作为 1σ。这是保守尺度，既没有按多点信息量缩减，也没有区分量化噪声和系统误差。后半段悬停抖动还可能影响方向拟合。不是 API 只有 0.7° 的固有角精度。

对已去重、截取外飞阶段的水平点作 TLS/PCA，令 t 为中心化沿线坐标、e 为垂直残差，采用小误差近似：

```
s² = max(Σe²/(n−2), q²/12)
σθ,random ≈ sqrt(s²/Σt²)          # 弧度，转换成度后再比较
σθ,total = sqrt(σθ,random² + σsystematic²)
```

量化误差未必独立，采样数不能直接替代有效独立包数；系统偏差也不能靠多采点消失。q²/12 是均匀舍入假设。本次仅以 0.1° 作为待验证的系统误差底限进行敏感性分析，不能当作已经标定的推荐固定值。跨度过小、轨迹弯曲、存在多实体或量化区间不一致时先拒绝观测。

| 投眼 | 旧 σ | 量化/拟合统计项 | 加示例系统底限后的 σ |
|---|---|---|---|
| 1 | 0.743° | 0.142° | 0.174° |
| 2 | 0.708° | 0.118° | 0.154° |
| 3 | 0.693° | 0.107° | 0.146° |
| 4 | 0.606° | 0.118° | 0.154° |
| 5 | 0.711° | 0.137° | 0.170° |

离线回放前两条重新拟合的观测，官方引擎仍将 `(-86,-74)` 排第一，模型概率约 99.99997%。这是同一批数据下的模型敏感性结果，没有独立真值，不能证明两眼实际准确率达到该数值。正式降低 σ 前应使用独立真值或留出数据检查误差分布、异常点与覆盖率，不能先从这批数据推定目标再拿它做真值。

证据在当前 BotSleep 仓库的 `logs/stronghold_efficiency_review.json` 和 `logs/stronghold_live_result.json`；日志不纳入 Git。此策略还没有替换运行时误差估计。

## 2. 投眼与选点

- 眼远离目标时通常飞约 12 方块，之后悬停；采满悬停阶段不会继续增加有效基线。持续检查有效跨度和方向稳定性，避免固定等待。
- 二维交会误差与射线交角有关。小交角时，沿距离方向的不确定性量级为 D²σθ/B；系数随几何、两次噪声而变化，这不是精确置信区间。
- 从安全可达的候选视点比较：剩余候选方位分离相对 σ 的大小、路程与通行成本、潜在受伤/恢复成本。侧移只是启发式，不应越过最近要塞分界；远距离先接近再补投可能比长横移更便宜。
- 两眼后若只剩两个候选，优先选能最大化它们角分离/观测噪声、且移动代价小的位置。没有明确歧义就不为凑投眼次数继续消耗。
- 截断的 top-k 概率未覆盖总质量时，不能把其归一化结果当作完整后验，也不能据此宣称置信椭圆很小。
- 距入口目标不足约 12 方块时，眼转为向下飞；水平跨度不足可能是近距信号。入口阶梯参考是区块 (4,4)，投眼目标为 (0,0)，不等于传送门房。

## 3. 运动方案为何需要独立设计

本地 API 包含 mineflayer-pathfinder 2.4.5。`move_to` 的公开参数为 x/y/z/range/entity_id/timeout，未公开 Movements 的避水代价等设置。不能把上游 Node 插件的配置字段随意塞给 HTTP 接口；需要在 harness 侧选择地形路点和做状态反馈。

本次寻路曾入河超时；受伤中断后角色继续下沉，说明“动作结束”不等于“安全”。曾把每次入水都判为失败，也导致浅水路线频繁中断；应区分浅水有支撑、表面游泳和潜没缺氧。恢复动作只解决当前位置风险，之后要重选路线，不要重复同一远目标。

`posture` 为全量状态，动作结束会重放持久姿态；`stop` 并不应被当成保持浮水的替代。上浮期间维持 jump，呼吸恢复且上岸后再解除。近岸用短键控和反馈，连续恢复没有进展就换路线。有船且工具支持时才评估水路，不假设当前背包有船。

## 4. 当前可用能力

| 能力 | 当前状态 |
|---|---|
| 实体识别、坐标采样、宏技能纯计算 | 已实现；Python 接口与计算工具 schema 存在 |
| 多点统计 σ、主动选观测点、地形成本导航 | 本 skill 的优化方案，未接入运行代码 |
| LLM 工具分发、Markdown skill 按需加载 | AgentLoop 仍待接入，不能声称已可自主执行 |

## 5. 来源（2026-11-13 查阅）

- [Ninjabrain Bot 官方说明](https://github.com/Ninjabrain1/Ninjabrain-Bot#how-it-works)：贝叶斯、测量误差及生成先验。
- [官方 1.5.2 Posterior 源码](https://github.com/Ninjabrain1/Ninjabrain-Bot/blob/1.5.2/src/main/java/ninjabrainbot/model/datastate/statistics/Posterior.java)：每条观测的标准差与似然更新。
- [Minecraft Wiki：末影之眼](https://minecraft.wiki/w/Eye_of_Ender#Locating_strongholds)：约 12 格外飞、悬停、近距下降和入口区块偏移。
- [mineflayer-pathfinder 官方文档](https://github.com/PrismarineJS/mineflayer-pathfinder#readme)：目标、路径、Movements 与停止机制；以本地包装后的实际 HTTP 契约为准。
- 本地 `mc-agent-api/src/session.js`：fmt、get_entities、use_item、move_to、posture、control；BotSleep 的 `integrations/ninjabrain/observations.py` 及本次真实记录。阈值与停止策略属于本文方案，不是上游保证。


## 6. Harness 绑定说明（不给小模型默认加载）

主 skill 只保留调用表和五步流程，推导、原始轨迹、研究资料由程序保存或按需提供。表中 Python 入口需要由 harness 绑定为工具；不是让游戏 AI 获得 Python 执行权限。当前通用分发和 Markdown skill 加载仍未实现，不能声称已经可自主调用。

```python
from integrations.ninjabrain.observations import EyeObservationCollector
from skills.stronghold import CalculateStrongholdSkill

# client 是同一个已登录的 MinecraftAgentClient，由 harness 注入。
o1 = EyeObservationCollector(client).capture()
# 移动并确认站稳后，再采第二眼。
o2 = EyeObservationCollector(client).capture()
result = CalculateStrongholdSkill().execute(
    throws=[o1["throw"], o2["throw"]], top_k=2,
)
```

HTTP 适配可复用 `client._checked_request(method, path, payload, timeout=...)`；网络等待时间（秒）应大于请求体中的动作 `timeout`（毫秒）。目前 `client.move_to()` 仍发送错误字段 `timeout_ms`，应使用表中的真实 HTTP 契约，不能声称旧封装已修复。所有 HTTP 调用检查 `ok`，技能调用检查 `success`，失败即返回错误。

给模型的采样结果保留观测与摘要即可，原始实体采样不展开到上下文。候选只返回前两项、状态只返回决策字段。会话、采样并发、拟合、误差估计和移动监控由 harness 执行，不让小模型手算或逐帧轮询。统计 σ 与导航优化仍为待实现方案，当前采样器行为不因本文精简而改变。

# Java 生存通关流程

这一流程供有下界、末地和末影龙的 Java 生存存档使用。它把装备、交通、资源、要塞和末地战斗接到现有 harness。每个步骤先观察，经过验证后再进入下一步。阶段和最近的结果会发布到实时监控。普通基地维护模式见 [SURVIVAL_MAINLINE.md](SURVIVAL_MAINLINE.md)。

## 启用

在本地配置中启用以下选项。任意现有模型配置都可以保留，通关控制器本身只运行本地目标和技能。用 `python main.py --config <文件> --check-config` 检查配置，再用同一配置启动。

```yaml
agent:
  mode: survival
survival:
  objective: daily
  intent: speedrun
  campaign: true
  continuous: true
  autonomy:
    enabled: true
```

`continuous` 用于取消整轮步数、时间和死亡上限，单次制作、探路和建门仍各有预算。手动停止有效。断线或 API 回复不明确时，检查点保留原意图并重新观察，旧写入不能直接补发。武器、食物、原料和已启动的本地任务根据重生后的观察重新决定。

## 战斗受击

受击处理优先于生产和模型规划。BotSleep 独立读取同一生命内的原生受击事件，区分战斗伤害与坠落等环境伤害。确认受袭后，中断当前生产动作，由本地规则读取装备、敌人和地形。远程威胁优先找实体掩体或侧移。条件允许时用武器和盾反击，近身受困时可以对实际确认的目标挥击一次，再检查撤退路线。长路线失败后改查短距离落脚点。近距离苦力怕会在爆炸前触发短撤退。未看见攻击者时，可以执行一次经地形核验的短侧移。每步之后重新观察，脱离危险后再继续生产。

实时监控显示「受击 · 本地规则优先」，并记录伤害来源、观察到的攻击者和所选动作。模型的工具列表移除了战斗撤退和反击。受击时旧模型回复会被作废，尚未结束的请求仍占用原有串行通道。

## 从开局到返回主世界

| 阶段 | BotSleep 的执行与推进依据 |
|---|---|
| 开局 | 复用本地制作目标，空手采木并制作工作台、木镐与石镐。缺露石时复用逐格核验的阶梯，足量后安排返程。 |
| 装备与补给 | 生命或饥饿较低时先备一份口粮。接着准备铁镐、铁剑、副手盾、水桶和 32 块圆石。装备齐备时补至 6 份口粮，下界补给低于 3 份则返程补给。追动物时使用核验过的落脚点，落水后先上浮再查找岸边。 |
| 下界门 | 复用已观察到的门，或制作打火石并使用现有熔岩铸门工作流。只有新维度的存活、就绪与坐标读回通过才记到达。记录两侧返回提示。 |
| 烈焰棒与珍珠 | 优先捡回目标掉落。利用烈焰人、下界砖、刷怪笼和诡异菌岩线索探索。缺珍珠时可狩猎末影人。已穿金甲且持有金锭时，可以同近处普通猪灵交易，确认金锭消耗，等待并检查背包或实际掉落。 |
| 返回与制作 | 补给低、装备丢失或材料充足时尝试返回主世界。用烈焰棒制粉，并将烈焰粉与末影珍珠组成末影之眼。默认准备 16 个眼当量，临近要塞时优先保留最多 12 个填框用眼。 |
| 要塞 | 跟踪真实投出的眼实体并拟合方向。两个样本有足够间隔且向前相交时，用交点缩短搜索。确认可达房间后，必须读到完整的 12 个朝内框位，逐个核对插眼前后方块属性及库存，再确认实际传送。 |
| 进末地 | 复用三维导航，缺平台通路时只在有明确末地石目标和当前支撑的情况下逐格搭桥。每次放置先确认背包消耗和新方块，下一轮才移动。 |
| 水晶与龙 | 从安全距离尝试射击实际可见的水晶，两次未消除同一目标时换站位。近处可读且可达的铁栅栏可逐格拆除。观测到龙息区域时优先移开。按实体距离选择射击或近战。 |
| 通关与回程 | 需要本次看见龙，且服务器屠龙进度从未完成变为完成。实体消失或动作回执不能作为通关证据。最后通过出口回到主世界，再发布完成状态。 |

住所选址先查地板。一旦已有缺失或危险支撑的直接证据就换点。通关模式最多在同一片地连续否决两个候选后触发换区，地表侦察先查 12 格、6 格通道，再按地形使用短步。通关观察跳过地形摘要、农田和草种专项扫描。

临时掩体只在时段内有附近敌人时封住。出口两格泥土由程序记录，威胁离开时会再次核验并清掉。永久住所默认用投影中的 11×9 木石大屋。通关模式使用 `survival.projection` 选择预设。设为空字符串时启用紧凑住所及有顶门廊方案。投影格式、备料和施工步骤见 [BUILD_PROJECTION.md](BUILD_PROJECTION.md)。

## AltoClef 的设计对应关系

| 参考模块 | 已应用的机制 |
|---|---|
| [BeatMinecraft2Task](https://github.com/gaucho-matrero/altoclef/blob/af22e3bc2f03dde45da703f5f7535baae18ea486/src/main/java/adris/altoclef/tasks/speedrun/BeatMinecraft2Task.java) | 用装备、食物、维度和库存选择下一段目标，按需求回补材料。 |
| [TaskRunner](https://github.com/gaucho-matrero/altoclef/blob/af22e3bc2f03dde45da703f5f7535baae18ea486/src/main/java/adris/altoclef/tasksystem/TaskRunner.java) 和 [ResourceTask](https://github.com/gaucho-matrero/altoclef/blob/af22e3bc2f03dde45da703f5f7535baae18ea486/src/main/java/adris/altoclef/tasks/ResourceTask.java) | 在现有 harness 中保留安全抢占与本地生产目标，优先使用已有掉落和已核实储备。 |
| [CollectBlazeRodsTask](https://github.com/gaucho-matrero/altoclef/blob/af22e3bc2f03dde45da703f5f7535baae18ea486/src/main/java/adris/altoclef/tasks/resources/CollectBlazeRodsTask.java) 和 [TradeWithPiglinsTask](https://github.com/gaucho-matrero/altoclef/blob/af22e3bc2f03dde45da703f5f7535baae18ea486/src/main/java/adris/altoclef/tasks/resources/TradeWithPiglinsTask.java) | 分别以烈焰人和末影珍珠的实际掉落为验收，给刷怪和交易有限等待时间。 |
| [LocateStrongholdCoordinatesTask](https://github.com/gaucho-matrero/altoclef/blob/af22e3bc2f03dde45da703f5f7535baae18ea486/src/main/java/adris/altoclef/tasks/movement/LocateStrongholdCoordinatesTask.java) 和 [GoToStrongholdPortalTask](https://github.com/gaucho-matrero/altoclef/blob/af22e3bc2f03dde45da703f5f7535baae18ea486/src/main/java/adris/altoclef/tasks/movement/GoToStrongholdPortalTask.java) | 第一颗眼后侧移，尝试用第二条及后续方向缩窄候选。只有真实方块构成的传送门才授权插眼。 |
| [KillEnderDragonTask](https://github.com/gaucho-matrero/altoclef/blob/af22e3bc2f03dde45da703f5f7535baae18ea486/src/main/java/adris/altoclef/tasks/speedrun/KillEnderDragonTask.java) | 分开到岛、处理水晶、战斗、验收和退出。BotSleep 的射击及挥击回执只表示对应动作，最终由服务器进度确认。 |

## HiyoriAI 的动作实现参考

| 参考实现 | BotSleep 中的改动 |
|---|---|
| [逐级搜索](https://github.com/luckui/HiyoriAI/blob/b60fb85ccf4b9de541be8cfd828504cf95b676ef/electron/minecraft/collectionStrategy.ts#L27) 与 [找木和排除失败点](https://github.com/luckui/HiyoriAI/blob/b60fb85ccf4b9de541be8cfd828504cf95b676ef/electron/minecraft/gatherWood.ts#L27) | 找木按 12、24、48、64 格逐级扩大。发现符合高度和树根条件的候选后结束扫描。有已观察到的树木或掉落时优先走可核验的短路径。原地没有可核验入口时，短期排除该列并换树。 |
| [寻路进展判据](https://github.com/luckui/HiyoriAI/blob/b60fb85ccf4b9de541be8cfd828504cf95b676ef/electron/minecraft/patchedGoto.ts#L71) | 定向移动执行一次后比较实际落点到目的地的距离。只有位移且距离没有有效缩短时记为该落点失败，下一次重新选路。 |
| [挖掘液体风险检查](https://github.com/luckui/HiyoriAI/blob/b60fb85ccf4b9de541be8cfd828504cf95b676ef/electron/minecraft/fluidSafety.ts#L18) | 初期开阶梯与深矿挖掘除相邻方块外，再查目标正上方最多 8 格的开放通道。遇实体隔层结束扫描，看到水、岩浆或缺少该段方块数据时拒绝开挖。 |

## 验证边界

- 当前端点可读取实体类型与位置，但没有暴露龙的完整停栖状态、晶柱笼子的导航语义或跨区块的完整地形。高笼、任意布局的虚空平台和长距离下界探索仍可能没有可用路径。
- 要塞导航依赖实测投眼和已加载的框位。深埋房间或当前扫描范围之外的框位需要更多找路能力。自动猪灵交易复用已穿金甲和现有金锭，狩猎是其余情况下的珍珠来源。
- 完成条件依赖服务器发送当地角色的屠龙进度。已完成过该进度的老角色、无龙存档、禁用进度的服务器不会通过本轮验收。
- 普通新档已验证空手开局、挡路树叶清除、阶梯采石、深采铁与返程、铁镐和铁剑制作、盾牌装备、狩猎做熟食、受袭后侧向逃离和击杀僵尸。铁桶阶段遇到连续死亡，实服仍在恢复，尚未进入下界。当前进度和回归记录见 [TESTING.md](TESTING.md)。

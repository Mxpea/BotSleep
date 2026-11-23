# Java 生存与速通流程：知识、阶段建议和执行边界

知识版本：`java_1_21_11 / schema_version=1`；资料访问日期：**2026-11-16**。

本轮只读核对相邻 `mc-agent-api/package.json` 为 **1.3.14**，其 `docs/RULES.md` 指定 **Minecraft Java 1.21.11**。项目历史实服记录为 Leaves 1.21.11；这不表示本轮重新连接服务器确认了所有 gamerule、插件或数据包。本文不采用 Bedrock、Minecraft Dungeons、D&D《Monstrous Compendium》PDF 或 Feedback 用户建议作为 Java 实现依据。

`core/survival_playbook.py` 是纯离线阶段建议器。下界探索、交易和末地战斗在本建议器的模型目录中仍显示为 `blocked`。在独立的通关模式中，这些阶段由本地控制器选用内部分支。启用方式、源码参照和现有验证见 [CAMPAIGN.md](CAMPAIGN.md)。

## 1. 给调用方的最短合同

```python
from core.survival_playbook import SurvivalPlaybook

playbook = SurvivalPlaybook(
    available_tools=actual_registered_skill_names,
    java_version="1.21.11",
    goal="survival",  # 或 speedrun；不会更改任务 objective
)
advice = playbook.build(fresh_observation, context)
```

返回 `schema_version/profile/goal/stage/state/prerequisites/missing/next_actions/risk_notes/bindings/unsupported/completed_evidence/source_ids`。标准 JSON 序列化不超过 **2048 字节**；最多 3 个候选动作，每个含 `name/args/reason`。`bindings` 只列本次候选实际使用的工具，不是全部能力目录。工具名必须同时属于调用方传入集合与模块已知合同；默认空能力集合不会产生动作。

`state` 的 `ready` 表示有当前可提交技能复核的候选，或进入持续维护；`prerequisites` 表示下一步只能补前置；`blocked` 表示证据、材料或适配不足。**没有 `goal_complete` 动作，也不以 `ready` 宣称永久生存或速通完成。** 所有建议仅用于下一轮规划，不能作为可重放队列。

`observation` 复用当前 collector 的 `status/counts/blocks/entities/furnaces/containers/shelter/daily_evidence/placement_candidates`。需要完整 `fresh=True` 观察；partial safety、未知库存、未登录、死亡、悬空或动作忙碌时不建议生产。`context.pending`、未知 `action_state` 或未确认挖掘/请求结果直接进入恢复门禁。低血先考虑现有食物；近敌交由本地 combat/retreat 门禁，不建议模型趁机采集或泼岩浆。

后期可选证据放在 `observation.progression_evidence`，每项至少满足：

```json
{
  "end_portal": {
    "fresh": true,
    "verified": true,
    "dimension": "overworld",
    "source": "api_verified",
    "empty_frames": 12
  }
}
```

接受的来源为 `api_verified/skill_verified/server_advancement`，并必须与当前维度相同。可识别 `return_portal/end_portal/armor_ready/dragon_defeated`。它们**是预留数据合同，不表示现有 collector 已能提供**；缺失、旧维度、记忆地点、模型自述均不能代替。`end_portal.empty_frames` 必须实际检查，范围 0–12。计算出的要塞坐标只能作为搜索候选。

如日后单独暴露 `plan_survival`，建议无写操作参数 `{goal:"survival"|"speedrun"}`，从 harness 已取得的当前观察构建上述返回值；本次模块没有注册此工具。现在无需新增一次模型/API 请求即可作为规划上下文使用。

## 2. 从空背包到可持续生活

| 阶段 | 实际流程及通过证据 | 当前能复用的动作 |
|---|---|---|
| `opening` | 读生命、饥饿、天气和地形；木头→木板→工作台/木棍→木镐→圆石→石镐/炉；同时取得并烹饪食物。只用库存实际净增确认材料。 | `gather_block/craft_item/place_item/hunt_animal/harvest_crop/smelt_*` |
| `iron_defense` | 石镐采铁，炉内装料、等待后检查产物；优先盾，再桶，再铁镐。正常配方盾为 6 木板+1 铁锭，桶为 3 铁锭，铁镐为 3 铁锭+2 木棍。防具和武器继续由日常任务图安排。 | `gather_resource/inspect_container/smelt_start/smelt_collect/craft_item` |
| `home_farm` | 选择可复查的营地，逐步建墙顶、门、床和箱子；准备水旁耕地、种子、收获/补种。保留繁殖种群与种子。 | 已有住所、储物、种植技能；自动围栏、诱导、繁殖和剪毛尚未绑定 |
| `maintenance` | 周期复查食物、装备耐久、炉子、住所和退路，缺什么回到对应前置。当前设施齐全不等于光照、长期产量或所有怪物风险已经验证。 | 复用 daily 任务图与本地安全策略 |

盾不是拿在背包里就生效：需要装备、使用并朝向来袭方向；有些攻击无法挡住，斧可使盾失效。[Minecraft：Shield](https://www.minecraft.net/en-us/article/taking-inventory--shield) 桶可运输水/岩浆，农场和后续下界准备均有价值；仅有铁锭或空桶不算已经有水。[Minecraft：Bucket](https://www.minecraft.net/ja-jp/article/taking-inventory-bucket)

动物和作物按可再生资源处理：牛、羊可以用小麦引导和繁殖；牛掉生牛肉、皮革，幼牛不会提供这些掉落。因此识别不到年龄时，不能把靠近并攻击算成有保证的食物来源。[Minecraft：Wheat](https://www.minecraft.net/en-us/article/taking-inventory--wheat)、[Minecraft：Cow](https://www.minecraft.net/zh-hans/article/cow) 猪使用胡萝卜、马铃薯或甜菜，不能把胡萝卜钓竿当繁殖食物；本条仅用社区 Wiki 的检索到的机制摘要作补充，不作为新自动繁殖技能的验收。[Minecraft Wiki：Breeding](https://minecraft.wiki/w/Breed)

成熟作物才收获，留下可再次种植的种子/作物。小麦种子来自草的随机掉落，`gained=0` 即没有得到种子；植物移除成功不等于食物生产准备完成。农田需要灌溉和光照条件；本项目的 `renewable=True` 只证明当前存在可再种植结构，未证明持续生长速度或永久安全。[Minecraft：Wheat](https://www.minecraft.net/en-us/article/taking-inventory--wheat)、[Minecraft：Carrot](https://www.minecraft.net/en-us/article/taking-inventory--carrot)

熔炼装料量同时受当前输入和燃料能力限制：普通煤/木炭按 8 个物品，主世界木板/原木按 1.5 个，木棍按 0.5 个核算；不会给 8 份原铁只装 1 根木棍。这里采用原版 Java 燃料表，定制配方/燃烧时间需重新核验。[Minecraft Wiki：Smelting](https://minecraft.wiki/w/Fuel)、[Planks](https://minecraft.wiki/w/Wood_planks)、[Stick](https://minecraft.wiki/w/Stick)

## 3. 狩猎、骷髅与苦力怕的策略边界

以下是实现技能需要满足的流程，不是绕过安全门禁的提示词。新工具能否使用，以实际运行目录、dispatcher 和调用方 `available_tools` 为准。

| 场景 | 合理流程 | 必须拒绝或退出的情况 |
|---|---|---|
| 猪/牛/羊/鸡 | 新鲜实体 ID 与种类→武器/血量/食物→支持与通道检查→有界接近及攻击→实际掉落入包。普通 `hunt_animal` 优先。 | 对象消失不能直接等同被杀；不攻击玩家、宠物、村民；幼体或归属不明不能假定可收获。 |
| 骷髅，有盾 | 盾进入正确槽位，面向目标并建立使用姿态；有界检查敌我位置、来袭方向和生命，再选择攻击窗口。 | 多方向敌人、地形未知、盾禁用/损坏、连续受伤。不能因 `equip` 成功就声称箭已被挡住。 |
| 骷髅，无盾 | 优先掩体和已验证退路；只有技能证明武器、距离、路线和生命允许时才做短段交战。 | 不穿越无遮蔽长距离追击；远程操作、射击和移动必须独立有界，不把“请求成功”当命中。 |
| 苦力怕，有盾 | 面向威胁并保持可退空间，依据新鲜距离/状态处理；盾只是减险手段。 | 未知点火状态、落崖边、周围财产/动物、多方向敌人。不得计划“站着等炸”并声称周边安全。 |
| 苦力怕，无盾 | 优先拉开距离/遮挡。短打退避须由技能证明进退条件，不能套固定“几刀必死”。 | 近距离连续点击、穿越身体、把岩浆当能瞬时阻止爆炸的办法。 |

Java 1.21.5 起官方 `blocks_attacks` 组件明确包含使用延迟、阻挡角度和伤害过滤；`weapon` 组件可以禁用阻挡。组件的缺省值不等于原版盾实例的实际值，不能从旧教程推出所有服务器相同的精确安全时限。[Java 1.21.5 技术更新](https://www.minecraft.net/en-us/article/minecraft-java-edition-1-21-5)

Mineflayer 的 `attack`、`activateItem(offHand)`、`deactivateItem`、`equip` 是底层原语，不能单独证明受到保护、目标死亡或物品拾取；完成证据仍需要当前实体、生命、库存与地形回读。其 `craft` 参数是配方执行次数，而 BotSleep 的 `craft_item.count` 是期望净增，二者不能混用。[Mineflayer 官方 API](https://github.com/PrismarineJS/mineflayer/blob/master/docs/api.md)

### 条件岩浆桶

`collect_liquid{pos,expected_name:"water"|"lava"}` 负责取源液；`hunt_with_lava{entity_id,expected_name}` 的用途是**已经存在的、不燃且可容纳液体的短时狩猎点**，不是任意地表铺岩浆。建议流程是：

1. 新鲜验证目标、主世界、源桶、完整容纳坑及旁侧/下方/退路；没有这些条件就使用普通狩猎或拒绝。
2. 排除木屋、树木、箱子、床、玩家及非目标生物附近；不通过“暂时看不到”推断无人。
3. 只做一次短点燃，立即回收源液，复查桶内容和世界液体状态。
4. 处理残火/逃离风险；直到确证场地不再有未知液体才允许后续生产。
5. 只把实际得到的食物计入产量；烧死、掉落烧毁、逃跑或回收未确认均不能假成功，未知写操作不得重放。

此策略约束来自本项目的保守执行设计，不是宣称原版提供“安全岩浆狩猎”保证。playbook 不会仅因背包里有岩浆桶就自动生成 `hunt_with_lava`。

## 4. 从主世界到末地的完整路线与未绑定部分

| 阶段 | 前置、执行顺序与证据 | 本轮实现边界 |
|---|---|---|
| `nether_preparation` | 稳定食物、盾/武器、镐、桶、点火、非易燃搭路材料、合适防具；记录主世界返回点。金防具对普通猪灵有用，但不是全下界免战。 | 现有合成、取水和装备可复用；模块的 12 份食物/32 方块是机器人保守储备策略，不是玩家速通最低配。 |
| `nether_portal` | 选择有足够源岩浆、支撑、操作空间与排水路径的地点；逐格浇筑并确认黑曜石；点火后确认传送门，最后验证维度切换和返回坐标。也可修复符合条件的废弃门。 | `portal_speedrun.py` 需要外部坐标和材料，仍非已绑定模型动作；缺自动选址、分步恢复及安全进入适配时 `blocked`。 |
| `nether_resources`：下界要塞 | 寻找真实要塞，确认安全进退路线；处理烈焰人和凋灵骷髅等威胁，获取并拾取烈焰棒。耐火药水可减少部分风险，但不是所有伤害免疫。 | 尚无通用下界导航、烈焰人战斗或耐火药水闭环；不能用打猪牛羊技能攻击烈焰人。 |
| `nether_resources`：堡垒/交易 | 堡垒遗迹可提供黄金和交易机会，但有蛮兵和复杂地形；可以选择更保守的散落猪灵交易路线。交易须记录金锭实际消耗、等待掉落、拾取与珍珠净增。 | 尚未绑定猪灵交易和堡垒路线；没有珍珠时仍是缺口。拿到黄金不等于已拿到珍珠，掉落数与次数不得写死。 |
| 返回与合成 | 保留可回主世界的门，核对坐标/维度；烈焰棒转粉，与珍珠合成末影之眼。模块目标为合计约 14 个眼当量，用于门与投掷余量。 | 合成已有材料可绑定 `craft_item`；下界进出仍缺适配。14 是规划余量，不保证足够，也不是强制机制。 |
| `stronghold` | 保存每次投眼的实际轨迹及误差；通过独立方向样本缩小候选范围，安全到达、寻找真实要塞和传送门房间。 | Ninjabrain 计算与投眼 CLI 独立存在；运行时采样、导航、路径确认尚未绑定。概率坐标不等于发现房间。 |
| `end_preparation` | 逐个检查 12 个门框已有眼，计算实际缺口；准备食物、武器、防具、弓箭、水桶和方块；确认重生/物品存放与进入后退路风险。 | 本模块可报告材料与证据缺口，但不会填眼或进入末地；弓箭、甲和方块数量是保守计划，不能代替战斗测试。 |
| `dragon` | 确认落点和虚空边缘；观察水晶及笼子；分阶段破坏水晶、规避龙息与击飞；飞行阶段远程、停栖阶段适用近战；最后确认服务端死亡和出口。 | 尚无水晶/龙阶段控制器、末地安全路线或完成验证。床爆炸一循环属于另一种高级技巧，未实现也不生成动作。 |
| `post_dragon` | 服务端明确完成证据后再确认出口，选择返回、继续探索或日常维护。 | “龙不在观察范围”不能当死亡；返回主世界也不自动意味着本机器人完成屠龙。 |

黑曜石与水、岩浆相关，但不能把任意流动岩浆或一次 API 放置返回当作已经浇筑正确。[Minecraft：Obsidian](https://www.minecraft.net/en-us/article/block-week-obsidian) 猪灵会因开容器、破坏黄金等行为敌对，普通金装备不是偷窃豁免。[Java Nether Update](https://www.minecraft.net/nb-no/article/nether-update-java) 堡垒遗迹和下界要塞是两种不同结构：前者有猪灵/蛮兵等守卫，后者是烈焰人资源路线。[Minecraft：Bastion remnant](https://www.minecraft.net/en-us/article/bastion-remnant)、[Minecraft：Nether Brick](https://www.minecraft.net/en-us/article/block-week-nether-brick)

烈焰粉与珍珠是末影之眼链条；获得烈焰棒的战斗风险不能被“做一个配方”掩盖。[Minecraft：Blaze Powder](https://www.minecraft.net/en-us/article/taking-inventory--blaze-powder) Java 要塞按环分布，投眼有破碎风险；最终门有 12 个框位，因此必须保留导航余量并实际检查已有眼数。[Minecraft：Stronghold](https://www.minecraft.net/en-us/article/stronghold)

保守屠龙路线先处理会为龙回血的水晶，再区分飞行与停栖状态；停栖阶段不能把射箭当有效近战替代。虚空、击飞和龙息需要独立安全处理。[Minecraft：Ender Dragon](https://www.minecraft.net/en-us/article/ender-dragon) 本文提供阶段依赖，不声称已有从出生到屠龙的实服闭环。

## 5. 版本差异和失败恢复

- **1.16.1 与现代 Java**：1.16.2 加入猪灵蛮兵并调整 barter 掉落。蛮兵不被黄金转移注意，也不因金防具停止攻击；不能复制 1.16.1 的堡垒风险、交易效率或固定珍珠数量预期。[Java 1.16.2](https://feedback.minecraft.net/hc/en-us/articles/360047630831-Minecraft-Java-Edition-1-16-2)
- **1.21.5+ 防御组件**：盾延迟、角度、可阻挡伤害与禁用行为由数据组件描述。客户端动作姿态不等于服务端伤害结算证明。[Java 1.21.5](https://www.minecraft.net/en-us/article/minecraft-java-edition-1-21-5)
- **1.21.11 环境与新武器**：水蒸发、快速岩浆、床使用/爆炸等迁到 Environment Attributes；维度、群系或数据包可能影响行为。本模块只配置 vanilla Java 1.21.11 概况，陌生版本直接 `blocked`；长矛及新生物也不自动套入旧剑/斧技能。[Java 1.21.11](https://www.minecraft.net/en-us/article/minecraft-java-edition-1-21-11)
- **床与下界**：主世界自动睡眠不能用于下界/末地；床爆炸不得通过普通 `auto_sleep` 触发。水桶的主世界灭火/落地经验不能直接用于下界。
- **死亡**：等待自然重生与 fresh 完整观察，按当前库存重建前置；只捡当前真实物品实体，不追着旧掉落坐标重放。记忆保存经验与候选地点，不保存“背包仍有铁器”的事实。
- **熔炼和容器**：`smelt_start` 是装料，不是成品；进行中先检查当前窗口。未知转移禁止再次装料/收取；明确失败可重观测后规划另一动作。
- **寻路/战斗/液体失败**：明确不可达才冷却目标并重观测；网络超时、挖掘未确认、源液回收未确认等保留未知状态。不能因一次结果不明便换路线发第二次写入。

## 6. 来源索引与本轮验证

模块 `source_ids` 对应：`java12111`→Java 1.21.11；`shield`→Shield；`bucket`→Bucket；`wheat`→Wheat；`cow`→Cow；`nether116`→Java Nether Update；`java1162`→Java 1.16.2；`blaze`→Blaze Powder；`obsidian`→Obsidian；`stronghold`→Stronghold；`dragon`→Ender Dragon。各页面链接位于上述相应机制段，全部于 2026-11-16 访问。历史科普文章只支持其具体机制，不作为 1.21.11 精确时序/掉率的证明。

本轮模块验证命令：

```powershell
python -B -m unittest discover -s tests -p test_survival_playbook.py -v
```

24 项纯离线回归通过：材料不足不虚构合成、盾桶铁器次序、当前副手/防具不重复制作、熔炼阶段与燃料量、健康门禁、观察到的动物种类、能力白名单、当前/历史证据隔离、未知结果不重放、后段未绑定、目标版本限制与 2 KB 输出上限。该数字不包括并行开发的战斗/桶技能测试，也不代表实服战斗、岩浆、下界或速通验收。

---
name: competition_scheduler
description: 竞赛任务动态决策与高性价比得分调度（非串行 PPM + 机会触发 + 及时止损）。
tags: [competition, planning, advancements, ppm]
---

# 竞赛调度规范

## 目标
以服务端 `/advancements` 为唯一计分真值，优先在有限时间内拿下单位时间得分（PPM）最高且不依赖高配装备的成就；禁止空耗在不通路线或缺少前置的成就上。

## 可用工具
| 工具 | 用途 | 关键规则 |
|---|---|---|
| `select_task` | 选择并执行 `CANDIDATES` 中的成就 | 传完整 `task_id`，立即挂载任务 |
| `update_progress` | 仅记录进度备注 | 不产生世界动作，禁止连续空调用 |
| `finish_task` | 向服务端校验成就完成 | 通过后切下一个目标 |
| `force_exit_task` | 放弃受阻或超时目标 | 进入指数退避，立刻释放调度位 |
| `end_competition` | 赛程结束收敛 | 仅在无剩余可执行高价值目标时调用 |

## 决策流程
1. **机会优先 (`OPP_*pt`)**：24m 内已发现羊毛羊、鸡群、骷髅、发光鱿鱼、水池或村民且前置满足时，优先拿下眼前的瞬时得分。
2. **高 PPM 与扇出 (`PPM_*pt`)**：优先 `mine_stone -> stone_pickaxe -> smelt_iron -> iron_pickaxe -> iron_boots(4铁)` 与 `plant_seed / breed_an_animal / kill_a_mob / fishy_business / deflect_arrow`，勿先追求全套铁/钻甲。
3. **传送门快通 (`PORTAL_GATE_READY`)**：铁镐或 4 铁锭就绪时优先入地狱解锁成簇高分项。
4. **残局收敛**：剩余时间不足时只选 `est_steps` 低的快攻项，避免开长链重工程。
5. **及时止损**：本地缺少对应生物/群系或连续失败时，立即 `force_exit_task` 转战高确定性成就。

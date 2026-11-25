---
id: iron_smelting
tags: [iron, raw_iron, iron_ingot, iron_pickaxe, smelt, furnace, speedrun]
---
# 铁矿采集与熔炉批炼

## 目标
高效获取至少 7 块铁锭（铁镐 3 + 铁桶 3 + 打火石 1）。

## 调用
- `run_goal(goal="acquire", item="iron_pickaxe", count=1)`
- `run_goal(goal="acquire", item="iron_ingot", count=4)`

## 步骤
1. 优先采洞口/地表暴露铁矿，不足时阶梯下挖至 Y=16。
2. 一次批量投料 `raw_iron` 与燃料（煤/木炭/木板），避免单块反复开关熔炉。
3. 凑齐铁镐后立刻检查下界门门控，不再刷冗余铁斧。

## 完成与中止
- 完成：`iron_pickaxe >= 1` 且具备铁桶与打火石材料。
- 中止：血量过低或装备耗尽先返表休整。

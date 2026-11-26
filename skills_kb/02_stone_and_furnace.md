---
id: stone_and_furnace
tags: [stone, cobblestone, stone_pickaxe, furnace, coal, daily, starter]
---
# 石镐与熔炉升级

## 目标
持有木镐后采集圆石，合成石镐与熔炉，准备采铁与烧铁。

## 调用
- `run_goal(goal="acquire", item="stone_pickaxe", count=1)`
- `run_goal(goal="acquire", item="furnace", count=1)`

## 步骤
1. 复用 16 格内已有工作台，禁止重复制造工作台。
2. 采集至少 11 圆石（3 石镐 + 8 熔炉）。
3. 合成石镐并立即准备开采铁矿与煤矿。

## 完成与中止
- 完成：背包持有 `stone_pickaxe` 与 `furnace`（或已放置熔炉）。
- 中止：连续 2 次阻挡拉黑该坐标并换点。

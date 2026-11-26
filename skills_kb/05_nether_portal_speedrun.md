---
id: nether_portal_speedrun
tags: [nether, portal, enter_nether, lava, obsidian, flint_and_steel, speedrun, portal_ready]
---
# 地狱门速通浇筑与进下界

## 目标
铁镐就绪后直接筹齐水桶、打火石并寻岩浆池浇筑点火，跨维进入 `the_nether`。

## 调用
- `run_goal(goal="enter_nether")`

## 步骤
1. 补齐 `iron_pickaxe`、`water_bucket`、`flint_and_steel`（挖砂砾刷燧石）。
2. 64 格范围扫描地表岩浆池，不足则螺旋探索；用多跳导航靠近，无 32 格上限。
3. 逐格浇筑 4×5 黑曜石框，清理门内临时圆石脚手架后点火跨门。

## 完成与中止
- 完成：`status.dimension == "the_nether"`。
- 中止：遇岩浆/敌怪由行为树硬安全层瞬时抢占，安全后无损续播。

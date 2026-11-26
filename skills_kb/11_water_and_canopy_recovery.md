---
id: water_and_canopy_recovery
tags: [water, swim, oxygen, canopy, leaves, stuck, pit]
---
# 浮水脱困与下树出坑

## 目标
防止落水溺毙、卡树冠死循环及地坑受困。

## 调用
- 本地行为树自动触发；若规划介入优先调用 `run_goal(goal="explore")` 或 `repair_step`。

## 步骤
1. 水下氧气 <= 10 时 `HardSafetyBranch` 仅发纯上升脉冲，恢复呼吸后再向前浮水登岸。
2. 脚下为树叶且高于地面时，地面僵尸豁免抢占，允许 `LocalRecoveryBranch` 破叶下树。
3. 深坑无路径时 `MultiHopNavigator` 自动切 `Pathfinder3D` 垫脚爬升或微掘。

## 完成与中止
- 完成：回到陆地安全立足点且氧气满格。
- 中止：未知写结果冻结。

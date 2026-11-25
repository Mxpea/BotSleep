---
id: combat_and_shield
tags: [combat, hostile, skeleton, zombie, shield, armor, sword]
---
# 短步战斗与护甲副手盾

## 目标
优先保全生存，穿戴最优护甲与副手盾，避免无脑追击远程怪或苦力怕。

## 调用
- `run_goal(goal="acquire", item="shield", count=1)`
- `run_goal(goal="acquire", item="iron_sword", count=1)`

## 步骤
1. 装备胸甲映射 `chest -> torso`，副手优先装备 `shield`。
2. 近敌由 `HardSafetyBranch` 反应式处理：苦力怕与低血一律撤退，健康近战可短步反击。
3. 位于高树冠或密封坑时，地面不可达僵尸/蜘蛛自动豁免抢占。

## 完成与中止
- 完成：威胁消失或完成装备升级。
- 中止：攻击结果未知时冻结，严禁补发二次攻击。

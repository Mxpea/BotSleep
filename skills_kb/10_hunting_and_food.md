---
id: hunting_and_food
tags: [food, eat, hunger, hunt, bread, animal]
---
# 食物储备与被动动物狩猎

## 目标
维持饱食度与血量恢复能力，避免饥饿拖垮探索与采矿。

## 调用
- `run_goal(goal="acquire", item="bread", count=4)`

## 步骤
1. 当 `food <= 14` 或低血缺饱食时，`LocalRecoveryBranch` 自动吃最高回复食物。
2. 背包无食物时优先就近狩猎牛/猪/羊/鸡或合成面包。
3. 生肉可放入熔炉批炼为熟肉提高饱和度。

## 完成与中止
- 完成：食物储备充足且 `food >= 16`。
- 中止：附近无动物且无农作物则向外围螺旋探索。

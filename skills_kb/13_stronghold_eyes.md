---
id: stronghold_eyes
tags: [stronghold, ender_eye, end, triangulation]
---
# 末影之眼采样与要塞定位

## 目标
在备足末影之眼后执行有界投眼采样并向要塞坐标多跳推进。

## 调用
- `run_goal(goal="locate_stronghold")`

## 步骤
1. 确认背包持有至少 2 颗 `ender_eye`。
2. 每次投眼后记录玩家坐标与朝向角，两次采样后交由三角定位估算要塞坐标。
3. 沿估算坐标分段 20 格多跳推进。

## 完成与中止
- 完成：到达估算要塞区域或进入 `the_end`。
- 中止：缺少末影之眼或遇强敌时先撤退补给。

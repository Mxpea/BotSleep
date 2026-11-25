---
id: cave_exploration
tags: [cave, explore, ore, torch, underground]
---
# 洞穴优先探矿

## 目标
优先采集洞口与浅层暴露矿物，降低盲挖耗时。

## 调用
- `run_goal(goal="mine", item="raw_iron", count=6)`

## 步骤
1. 扫描 32 格内暴露矿脉并优先挖净整脉。
2. 不可达矿格失败 2 次自动拉黑冷却，不再原地空转。
3. 被困深坑时允许 3D 寻路使用圆石垫脚爬升返表。

## 完成与中止
- 完成：矿量达标或发现目标结构。
- 中止：深洞多怪且低血时立即撤退。

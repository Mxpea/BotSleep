---
id: shelter_and_night_sleep
tags: [shelter, night, sleep, bed, structure]
---
# 四朝向庇护所与夜间入睡

## 目标
夜间或受威胁时就近入睡，或按统一朝向 `StructureSpec` 单步补建庇护所。

## 调用
- `run_goal(goal="build_shelter")`

## 步骤
1. 夜间 12541–23458 若 16 格内有安全床位，`LocalRecoveryBranch` 自动靠近并入睡。
2. 建房按 `StructureSpec` 四朝向蓝图自检缺块，每帧只补 1 块。
3. 庇护所墙体、门、床、箱加入 `avoid_block_breaking` 保护集，寻路禁拆自家墙。

## 完成与中止
- 完成：成功入睡度夜或庇护所全部方块就位。
- 中止：有可达敌怪先由安全层退避。

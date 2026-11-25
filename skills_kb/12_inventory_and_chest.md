---
id: inventory_and_chest
tags: [inventory, full, chest, drop, storage]
---
# 背包清理与箱子存取

## 目标
保持至少 3 个背包空位，避免采矿或合成掉落无法拾取。

## 调用
- `run_goal(goal="acquire", item="chest", count=1)`

## 步骤
1. 空位不足时优先按低价值顺序丢弃腐肉、毒马铃薯、多余种子与杂石。
2. 重要物资（铁锭、钻石、黑曜石、打火石）严禁丢弃，可存入坐标已记录的箱子。
3. 存取使用 `window/act` 有界批次操作。

## 完成与中止
- 完成：`empty_slots >= 3`。
- 中止：无低价值物品可丢弃时放置箱子转存。

---
id: wood_and_tools
tags: [opening, wood, oak_log, crafting_table, wooden_pickaxe, daily, starter]
---
# 开局砍木与木镐

## 目标
空手出生后快速获取 4 原木，合成木板、1 张工作台与木镐。

## 调用
- `run_goal(goal="acquire", item="wooden_pickaxe", count=1)`

## 步骤
1. 自动扫描最近暴露原木并挖掘。
2. 手合木板与 1 张工作台，确认 16 格内无重复工作台后再放置。
3. 合成木棍与木镐，校验背包增量。

## 完成与中止
- 完成：背包持有 `wooden_pickaxe >= 1` 或更高阶镐。
- 中止：周边无树时自动切入螺旋探索；若发生 `unknown` 写冻结则暂停。

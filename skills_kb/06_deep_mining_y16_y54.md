---
id: deep_mining_y16_y54
tags: [mining, iron_ore, diamond, coal, shaft, y16, y54]
---
# 阶梯采矿与重力块安全

## 目标
按矿层高度（铁 Y=16、钻石 Y=-54）单步掘进并保留返表通道。

## 调用
- `run_goal(goal="mine", item="raw_iron", count=8)`
- `run_goal(goal="mine", item="diamond", count=3)`

## 步骤
1. 先确认目标矿所需镐阶（铁需石镐、钻需铁镐）。
2. 开挖头顶或邻格前，网关对砂砾/沙子与流体强制 3 秒新鲜校验。
3. 每帧仅发 1 次挖掘或移动，遇危险立即归还控制权。

## 完成与中止
- 完成：目标矿物数量达标。
- 中止：背包空位 <= 2 或血量/饱食度告警时先休整。

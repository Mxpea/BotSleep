---
id: competition_scoring
tags: [competition, advancement, score, select_task, finish_task]
---
# 赛事成就选择与服务端真值核验

## 目标
按分值/步数性价比选择可执行无条件成就，严格以服务端 `/advancements` 为唯一计分真值。

## 调用
- `select_task(task_id)` / `update_progress(note)` / `finish_task()` / `force_exit_task(reason)` / `end_competition(reason)`

## 步骤
1. 仅可从当前可计分且已打通路线的成就中选择（无显式 override 严禁选条件项）。
2. 选中后挂载对应 L3 任务执行，完成后必须调用 `finish_task` 经服务端 `/advancements` 核实。
3. 遇长期不可达项调用 `force_exit_task` 延后冷却并切换次优高分项。

## 完成与中止
- 完成：服务端返回 `status == "completed"` 计分累加。
- 中止：超时或无剩余可选任务时调用 `end_competition`。

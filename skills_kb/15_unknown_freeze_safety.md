---
id: unknown_freeze_safety
tags: [unknown, pending, frozen, safety, watchdog, needs_ai]
---
# 未知写入冻结与故障恢复协议

## 目标
当写动作超时或断连导致状态未知时死守冻结红线，不重放、不追加清理写。

## 调用
- `wait(reason="pending_unknown_frozen")`

## 步骤
1. `PendingUnknownGuard` 置顶拦截全部写入，禁止补发 `posture`/`stop`/重复动作。
2. 仅允许只读观测刷新黑板，等待人工核实或确认世界状态重置后解锁。
3. 非冻结类的局部失败（`NEEDS_AI`）先阅读 3 行 DEPS 根因诊断再切换替代目标。

## 完成与中止
- 完成：事务锁合法清除后恢复规划。
- 中止：冻结期内严禁下发任何写入类工具。

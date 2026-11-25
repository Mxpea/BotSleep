---
name: stronghold-localization
description: harness 游戏 AI 的少投眼定位与移动流程。
---

# 要塞定位

目标：尽量两眼定位，少走路。下表由 harness 执行；未提供对应工具就报缺失。HTTP 基址为 `/api/v1`，复用当前会话。

## 调用表

| 操作 | 具体入口 |
|---|---|
| 状态、背包 | `GET /status`、`GET /inventory` |
| 采一眼，保存为 o | `EyeObservationCollector(client).capture()` |
| 算前两候选 | `CalculateStrongholdSkill().execute(throws=[o1["throw"],o2["throw"]],top_k=2)` |
| 移动 | `POST /move_to {"x":X,"z":Z,"range":2,"timeout":8000}`；timeout 单位毫秒 |
| 停止 | `POST /stop {}` |
| 上浮、解除 | `POST /posture {"jump":true}`、`{"jump":false}`；会清除其他姿态 |

HTTP 检查 `ok`；计算检查 `success`，候选在 `data.candidates`。采样和计算入口需 harness 绑定，不能假设已注册。

## 五步流程

1. 确认主世界、空闲、有眼；站稳采第一眼。拟合、方向换算和 σ 交给程序，不自行降低 σ。
2. 选安全陆地点，横向移动约 20～50 格，再采第二眼；避河流，短路点行走，移动失败后重读实际位置。
3. 计算。误差有效且首选 ≥95% 就停；仍有歧义才换位置补眼。眼向下飞或跨度不足时检查入口，不连续重投。
4. harness 每 0.3 秒监控；卡住 3 秒、受伤或潜没则停止。潜没先持续上浮，上岸后解除 jump，再换路线。
5. 输出 `chunk/eye_target/dig/probability`。`dig` 是入口参考，未确认传送门房或 Y 就注明。

---
id: bucket_and_fluid_los
tags: [bucket, water_bucket, lava_bucket, fluid, target_occluded, interact]
---
# 桶装液体与视线清障

## 目标
使用 `/interact` 安全取水或取岩浆，消除岸边 `y+0.85` 视线遮挡。

## 调用
- `run_goal(goal="acquire", item="water_bucket", count=1)`
- `run_goal(goal="acquire", item="lava_bucket", count=1)`

## 步骤
1. 空桶取液走 `interact(pos, item="bucket")`，严禁用 `use_item`。
2. `FluidPrimitive` 自动校验 `y+0.85` 射线；若被岸边草/单格泥土遮挡，先清障再站位装液。
3. 2×2 水池为无限水源；连续 2 次失败记入不可达黑名单换源格。

## 完成与中止
- 完成：背包确认获得 `water_bucket` 或 `lava_bucket`。
- 中止：未知写结果冻结重放。

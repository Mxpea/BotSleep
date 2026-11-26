# BotSleep v2 Tokens 使用与 AI API / mc-agent-api 异常报告（2026-11-26）

- **统计范围**：`single_comp_r1`、`single_comp_r1b`、`single_daily_r2`、`single_daily_r3` 与 `v2_live9`（9-Bot × 1800s），合计 **5 场实服 / 13 个 Bot 实例 / ~4.96 Bot·小时**
- **数据来源**：`v2_flight_*.jsonl`（BotSleep 客户端遥测 + `TICK_SUMMARY` 累计账单）及 `mc-agent-api` 进程标准输出日志（`task-1200.log` 与 `task-1461.log`）
- **安全脱敏**：报告不含任何完整 API Key 或登录密码；`config.yaml` 默认 `llm.overflow.enabled: false` 未改动，备用额度（`https://doro.lat/v1`）**`0` 次调用消耗**

---

## 1. Tokens 使用总账与双 API 路由表现

### 1.1 总账对比（主路 vs 溢出备用）

> **统计口径说明（重要）**：实服压测期间发现 `v2/app.py` 的 `LLM_RESPONSE` 事件仅在主线程采纳并应用提案时记录（共 `250` 条），若 `AsyncLLMReplanNode` 期间发生目标状态切换导致过期提案被丢弃，该次真实完成的 HTTP 请求会累计入 `OpenAIBrain.primary_calls` / `total_prompt_tokens`（写入 `TICK_SUMMARY` 与 `MODEL_SLOT_ACQUIRED`）而未触发 `LLM_RESPONSE`（例如 `bot_3` 在 `v2_live9` 实发 `60` 次而仅应用 `12` 次）。下表按 **真实计费口径（`TICK_SUMMARY` / `MODEL_SLOT`）** 与 **已应用口径（`LLM_RESPONSE`）** 分别列明，且此遥测缺口已在提交 `7b14b12` 中通过 `LLM_HTTP` 全量事件补齐。

| 会话 / 轮次 | 模式 | 时长 | 主路真实请求数 | 主路已应用数 | 溢出请求数 | 真实 Prompt Tokens | 真实 Completion Tokens | 真实 Total Tokens | 实返确认模型 (`response.model`) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `single_comp_r1` | competition | `290.55s` | `114` | `114` | `0` | `155,073` | `4,485` | `159,558` | `qwen3.6-35b-a3b` |
| `single_comp_r1b` | competition | `274.41s` | `17` | `17` | `0` | `26,265` | `886` | `27,151` | `qwen3.6-35b-a3b` |
| `single_daily_r2` | daily+speedrun | `561.73s` | `19` | `13` | `0` | `26,836` | `1,133` | `27,969` | `qwen3.6-35b-a3b` |
| `single_daily_r3` | daily+speedrun | `491.09s` | `8` | `8` | `0` | `10,232` | `489` | `10,721` | `qwen3.6-35b-a3b` |
| `v2_live9` (9-Bot 汇总) | daily+speedrun | `1808.78s` (max) | `180` | `98` | `0` | `250,660` | `10,247` | `260,907` | `qwen3.6-35b-a3b` |
| **全夜总计** | — | **`~4.96 Bot·h`** | **`338`** | **`250`** | **`0`** | **`469,066`** | **`17,240`** | **`486,306`** | **`qwen3.6-35b-a3b` (100%)** |

- **Prompt : Completion 比例**：`469,066 : 17,240 = 27.21 : 1`；单次请求平均开销 **`1,388` prompt / `51` completion / `1,439` total tokens**（受严格 `<= 6,600B` `SemanticCompactor` 与 `max_tokens=512` 约束）。
- **与旧版 v1 (`v31`) 同等单位时间用量对比**：旧版 v1 9-Bot 在 `90 min` 内消耗 `20,372,954` tokens（折合每 30 min 约 `6.79M` tokens、约 `669` 次请求）；v2 借助 L2/L3 长视野 BT 自主任务，9-Bot 30 分钟仅消耗 **`260,907` tokens（减少约 `96.2%` / ~26 倍）**，极大降低了 API 成本与排队拥塞。
- **为什么 `v2_live9` 开启 `overflow.enabled: true` 仍出现 `overflow = 0`**：
  经复核 [v2/app.py](../v2/app.py) 与 [v2/kernel/model_gate.py](../v2/kernel/model_gate.py)，9 个进程确实共享 `%TEMP%/botsleep-model-gates` 下按主路 `base_url + api_key` 哈希的跨进程全局 FIFO 锁（`global_concurrency=True`、`primary_wait_seconds=1.5s`）。由于 9 个 Bot 合计每分钟仅产生 **~6.0 次**请求（`180` 次 / `30` 分钟），且主路中位响应约 `1.53s`，绝大多数请求在 `1.5s` 排队等待窗口内直接获得主路槽位并成功返回，未触发 `ModelGateError("llm_busy")`，因此 **100% 保留了备用 API 额度**。

### 1.2 AI API 延迟分布（基于 250 条带精确定时 `LLM_RESPONSE` 记录）

| 批次 | 样本数 `n` | `p50` 延迟 | `p95` 延迟 | `max` 延迟 | 运行期 `LLM_ERROR` |
| --- | ---: | ---: | ---: | ---: | ---: |
| `single_comp_r1` | 114 | `995.5ms` | `1,491.7ms` | `2,354.0ms` | 0 |
| `single_comp_r1b` | 17 | `1,519.0ms` | `2,763.8ms` | `2,939.0ms` | 0 |
| `single_daily_r2` | 13 | `1,488.0ms` | `2,204.2ms` | `2,851.0ms` | 0 |
| `single_daily_r3` | 8 | `1,458.5ms` | `2,763.1ms` | `3,244.0ms` | 0 |
| `v2_live9` (9-Bot 并发) | 98 | `1,529.5ms` | `2,416.3ms` | `2,703.0ms` | 0 |
| **合并总计** | **250** | **`1,156.0ms`** | **`2,324.0ms`** | **`3,244.0ms`** | **0** |

### 1.3 `v2_live9` 单 Bot AI 请求分解

| Bot | 真实请求数 | 已应用数 | 真实 Prompt | 真实 Completion | 真实 Total | 丢弃率（未应用占比） | 主要诱因 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `SleepBot1` | 12 | 8 | 16,308 | 812 | 17,120 | 33.3% | 状态切变过期提案丢弃 |
| `SleepBot2` | 10 | 8 | 13,300 | 604 | 13,904 | 20.0% | 死亡/重连切变 |
| `SleepBot3` | **60** | 12 | **88,299** | **3,220** | **91,519** | **80.0%** | 矿道 `place:no_support` 与 `NEEDS_AI` 高频重规划与并发抢占丢弃 |
| `SleepBot4` | 11 | 6 | 14,502 | 636 | 15,138 | 45.5% | `move_to:timeout` 后抢占 |
| `SleepBot5` | 22 | 19 | 31,116 | 1,299 | 32,415 | 13.6% | 多阶段任务正常切换 |
| `SleepBot6` | 6 | 4 | 7,462 | 312 | 7,774 | 33.3% | 后半段卡在 `bot_6` 伪进展中停止请求 |
| `SleepBot7` | 30 | **28** | 41,439 | 1,586 | 43,025 | 6.7% | 全队推进最快（多步合法任务切换） |
| `SleepBot8` | 9 | 7 | 11,917 | 539 | 12,456 | 22.2% | 正常 |
| `SleepBot9` | 20 | 6 | 26,317 | 1,239 | 27,556 | 70.0% | 遭遇溺尸受伤/死亡导致多次重规划被中断 |

---

## 2. AI API 异常调用与格式问题报告

1. **【预检阶段阻塞】用户消息所附主路 Key 鉴权失效（HTTP 401）**：
   - 用户最初消息中给出的主路 Key（已脱敏：`sk-...ddG`）对 `https://api.nju-aia.com/v1/chat/completions` 与 `/models` 均返回 `HTTP 401 Invalid token (new_api_error)`。
   - **处理**：验证本机既有效 Key（来自 `runtime/nether_bench_v31/configs/bot_1.yaml`，`GET /models` 实返 `200` 含 `default` / `qwen3.6-35b-a3b`）并写入未跟踪的 `config.yaml`；整夜实测全程 **0 次 HTTP 401 / 429 / 5xx**。
2. **【实服运行期】工具参数格式/语义偏差（已全部完成服务端/编译器防御）**：
   - **R1 提示词拼接 `x1`**：`HorizonSelector.format_top()` 多拼 `x1` 导致小模型 `select_task` 连续 114 次拒收（`5bd6766` 修复）。
   - **R1b 空闲连发 `update_progress`**：小模型连续 4 次只打进度注释而不调用 `select_task`（`c53d3ad` 修复）。
   - **R2 `item="furnacex1"` 回声拼成 `furnacex1x1`**：模型照抄提示词 `acquire:furnacex1` 作为 `item` 入参，连败 4 轮（`c696764` 新增 `_normalize_item_and_count()` 修复）。
   - **R3 偶发传 `iron_ore`**：1 次传 `acquire:iron_ore`（原版应采 `raw_iron`），随即在下一轮模型自行改回 `iron_pickaxe`。
3. **【遥测缺口】异步丢弃调用未记录 `LLM_RESPONSE`**：已在 `7b14b12` 于 `OpenAIBrain._call_single` 补入无损 `LLM_HTTP` 记录。

---

## 3. `mc-agent-api` 调用统计与异常分析

### 3.1 接口使用率与进程稳定度

- **代码静态绑定率**：[v2/kernel/client.py](../v2/kernel/client.py) 封装上游全量 **29 个路由**（27 个会话接口 + 2 个 auth 路由），`v2/` 代码调用其中 **28 / 29** 个（仅 `POST /look` 未调用）；本夜真实在线调用覆盖 **17 个独立端点**（单 Bot `advancements` + 双 API 压测全套感知/动作端点）。
- **并发压力与稳定性（单进程 `:18900` 托管 9 Worker）**：
  - `task-1461.log`（9-Bot 30 分钟会话，日志总行 `286,623` 行）实录上游完成 **`285,829` 次 `API OK`**（其中并发池/批读触发 `get_block` **`240,922` 次**、`find_blocks` **`12,725` 次**、`status` **`6,731` 次**、`inventory`/`events`/`get_entities` 各 **`6,628` 次**），出现 `16` 次重生/登录 `WORLD ready`（`p50 = 3,522.5ms`, `max = 4,802ms`）。
  - 单 Bot 会话（`task-1200.log`）实录 **`12,928` 次 `API OK`**，5 次 `WORLD ready`（`p50 = 3,726ms`, `max = 4,081ms`）。
  - **Node 主进程与各 Bot Worker 全夜 0 次崩溃、0 次 OOM、0 次队列死锁**。

### 3.2 BotSleep `FlightRecorder` 记录的逐端点耗时与失败率（`v2_live9` `API_TIMING` 全量 `35,779` 次顺序/主调请求）

| 端点 | 总调用数 | 成功 | 失败 | 失败率 | `p50` | `p95` | `max` | 说明 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `get_block`（顺序直调） | 16,904 | 16,904 | 0 | 0.00% | `1.0ms` | `1.0ms` | `25.0ms` | 另有 `~224k` 经并行读池发出 |
| `find_blocks` | 12,716 | 12,716 | 0 | 0.00% | `3.0ms` | `14.0ms` | `60.0ms` | 服务端块扫描极快且零报错 |
| `move_to` | 2,769 | 2,252 | 517 | **18.67%** | `295.0ms` | `8,004.0ms` | `10,057.0ms` | 含 `path_not_found` 与 `8s` 超时 |
| `dig` | 2,491 | 2,476 | 15 | 0.60% | `1,520.0ms` | `9,157.0ms` | `23,397.0ms` | 含远端靠近+挖掘总耗时 |
| `control` | 475 | 475 | 0 | 0.00% | `459.0ms` | `508.0ms` | `528.0ms` | 微步跃障 / 游泳脉冲 |
| `craft` | 166 | 166 | 0 | 0.00% | `533.0ms` | `1,333.2ms` | `1,555.0ms` | 100% 稳定成功率 |
| `window/view` | 132 | 132 | 0 | 0.00% | `708.5ms` | `1,222.3ms` | `2,137.0ms` | 熔炉轮询读取 |
| `place` | 91 | 49 | 42 | **46.15%** | `378.0ms` | `1,300.5ms` | `1,404.0ms` | 矿道缺支撑位置占比高 |
| `window/act` | 16 | 16 | 0 | 0.00% | `1,284.5ms` | `1,559.2ms` | `1,722.0ms` | 放料/取铁 100% 成功 |
| `status`（顺序校验） | 9 | 9 | 0 | 0.00% | `5.0ms` | `24.4ms` | `26.0ms` | 启动握手 |
| `auth/logout` | 9 | 9 | 0 | 0.00% | `1.0ms` | `2.0ms` | `2.0ms` | 全员正常登出 |
| `use_item` | 1 | 1 | 0 | 0.00% | `2,045.0ms` | `2,045.0ms` | `2,045.0ms` | 进食 |
| **总计 (`API_TIMING`)** | **35,779** | **35,205** | **574** | **1.60%** | — | — | — | 单 Bot 4 轮另有 2,000 次 `API_TIMING`（含 `advancements` 26/26 成功） |

### 3.3 `mc-agent-api` `API_ERROR` 错误码明细与对策（`v2_live9` 共 `574` 次）

| 端点 : 错误码 | 次数 | 占比 | 典型返回信息与根本原因 | 处理建议（仅在 BotSleep 侧适配） |
| --- | ---: | ---: | --- | --- |
| `move_to:path_not_found` | **265** | 46.2% | `"No path to the goal!"` —— 树冠/复杂洞穴中目标可达性先验不足 | 已在 `7b14b12` 收紧 6 拍零位移拉黑阈值与 L4 换目标触发 |
| `move_to:timeout` | **182** | 31.7% | `"pathfinding timed out after 8000ms"` —— 长距/多高度差 pathfinder 超时 | 缩短复杂地形单跳 relay 距离或提前升级 3D A* |
| `place:no_support` | **35** | 6.1% | `"no free spot with solid ground within 3 blocks"` —— `bot_3` 在 1×2 矿道放桌/炉失败 28 次 | 建议在 `PlacePrimitive` 失败时尝试旁挖 1 格凹室再放 |
| `move_to:interrupted` | **33** | 5.7% | `"stopped because of hurt"` —— 行走中遭僵尸/溺尸打断 | 正常战斗打断语义，由 `HardSafetyBranch` 接管 |
| `move_to:not_ready` / `dig:not_ready` | **24** | 4.2% | `"world is still loading; retry in a moment"` —— 死亡重生后约 `3.5s` 区块重载窗 | 建议对 `not_ready` 做短睡眠等待而非计入 `last_failure_reason` |
| `move_to:stalled` / `dig:stalled` | **16** | 2.8% | `"pathfinder made no progress for 8s"` —— 实体碰撞箱/墙角阻塞 | `Unstuck` L1–L3 已能覆盖多数场景 |
| `place:place_failed` | **7** | 1.2% | `"Server refused to place ... the block is still air"` —— 客户端站位微偏移或反作弊拒判 | 换邻格/清理脚下或轻微后退 |
| `dig:interrupted` | **5** | 0.9% | `"stopped because of hurt"` | 正常被击中断 |
| `move_to:dead` | **4** | 0.7% | `"stopped because of death"` | 正常死亡后状态切变；已通过 `WORLD ready` 自动恢复 |
| `dig:dig_unconfirmed` / `path_not_found` | **3** | 0.5% | 挖掘确认超时或无路径 | 与 `not_ready` 类似，重采位姿重试 |

- **单 Bot 轮 `API_ERROR` 补充**：4 轮单 Bot 共记录 `45` 次 `API_ERROR`，其中 `17` 次为启动前 `status:not_connected`（登录异步等待期间轮询，已建议适当拉长退避）、`14` 次 `move_to:path_not_found`、`8` 次 `move_to:timeout`、`2` 次 `place:place_failed`、`2` 次 `dig:interrupted`。**重要结论：整夜 5 场运行中 `action_state == "unknown"` 的未知写入不确定异常为 `0` 次！**

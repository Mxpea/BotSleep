# 调试、日志与实服靶场

更新：2026-11-22。普通运行方式见 [README.md](../README.md)，本轮结果及历史证据见 [TESTING.md](TESTING.md)。

## 聊天调试入口

启用 `--debug` 或 `debug.enable_in_game_commands: true`，并配置 `debug.allowed_usernames`。空名单不接受命令；只处理白名单玩家的 `<Player> #command`，忽略服务器/匿名消息和机器人自身消息。competition 模式禁止聊天调试。

| 命令 | 参数 / 条件 | 行为 |
| --- | --- | --- |
| `#help` / `#status` / `#inv` | 调试已开启 | 列出实际注册命令、运行状态或背包计数 |
| `#stop` | 调试已开启 | 请求取消并暂停；不能证明服务器动作已瞬时结束 |
| `#sleep` | 已注入睡眠回调，主入口有 | 交由控制器安排睡眠 |
| `#resume` | 生存模式注入恢复回调 | 重新观察后规划，不重放 pending，不清模型请求锁 |
| `#radar on/off/scan/poi` | 启动时额外传 `--radar` | 开关后台扫描、立即扫描或查询开发记忆 |
| `#chop [数量] [秒]` | 另行注入 `chop_skill` | 当前 main 未注入，因此普通 `--debug` 不注册 |
| `#craft <物品> [数量]` | 另行注入 `craft_skill` | 当前 main 未注入；不是模型的 `craft_item` |

```powershell
python -B main.py --config config.yaml --debug --radar
```

雷达确有后台 daemon 线程，约每秒读状态，默认移动 6 格触发扫描；单独 `--radar` 无效。其 MemoryEngine 默认写 `data/game_logic/`，不是生存 SQLite 或 scoped 检查点。独立宏技能与聊天回调不自动获得 harness 动作互斥或取消恢复保证，不要与生存动作同时使用。睡眠/生存控制器的后台线程保证不能推及所有调试 handler。

## 日志、恢复与只读监控

FlightRecorder 把显式发布事件写入配置的日志目录，路径相对配置文件解析；不保证每次异常都有完整三维现场。生存运行记录 DECISION、STEP_RESULT、SAFETY_INTERRUPT、DEATH_OBSERVED、RECOVERY_REQUIRED、PAUSED 等事件，检查点按 API/账号/world_id 隔离。

游戏 pending 必须先重新观察、核验，不自动重放；`--resume` 不解除模型未知锁。只有确认远端模型已空闲后才能使用 `--acknowledge-model-idle`，不能因本地超时或取消就试探解锁。停止回执不是动作结束证据，清理失败也不应跳过登出。详细契约见 [SURVIVAL.md](SURVIVAL.md) 和 [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md)。

`debug.monitor.enabled: true` 提供默认 `127.0.0.1:18930` 的只读网页，只消费已发布事件并脱敏、限制缓存。它显示阶段、任务、模型槽、response.model 和执行结果，不是远程控制台，也不重试动作或清锁。门户 PortalTelemetryRecorder 主动读游戏并导出 JSON，是另一条开发记录路径，未接入该事件面板；见 [MONITORING.md](MONITORING.md)。

## 常见踩坑与排查清单（开发者必读）

结合端侧实服日志（如 `flight_20261122.jsonl`）和多 Bot 联调复盘，调试和跑测时务必注意以下 4 类高频陷阱：

1. **误用 `starter` 模式与默认严格睡眠策略**
   - **现象**：上下文刚到 30KB 就频繁触发 `LLM_HISTORY_COMPACTED`（丢弃历史轮次）；没有 `AutonomousTaskController` 和 `SurfaceScoutSkill`，机器人退化为大量小步 `move_to`，容易被实体碰撞箱卡住或尝试徒手挖圆石失败；若遇黑夜/雷暴且无床，会连续输出 `SAFETY_OVERRIDE`（`wait(5)`）原地空转。
   - **规避**：生存长时间运行/联调必须使用 `agent.mode: survival`、`survival.objective: daily`（或命令行 `--mode survival --objective daily`），并在配置中设置 `survival.sleep_policy: "opportunistic"`。

2. **端侧 `llm.timeout_seconds` 过小与 `ModelRequestGate` `uncertain` 锁死**
   - **现象**：端侧模型在高上下文字节（实测曾达 ~28.9KB）下推理耗时会超过 20s（如 21.08s），超时后触发 `llm_timeout` 并写入持久化的 `MODEL_REQUEST_UNCERTAIN` 状态；此后简单重启程序会在 15ms 内再次报错拒跑，因为重启不会自动清除 `uncertain` 锁（防止幽灵并发请求）。
   - **规避与恢复**：保持 `llm.timeout_seconds >= 60`（与 `core/config.py` 默认值一致）；若遇 Windows/代理 TLS 握手异常可显式配置 `llm.tls_version: "1.2"`。一旦触发 `MODEL_REQUEST_UNCERTAIN`，**先确认远端模型服务端已无在途请求**，再使用 `--acknowledge-model-idle` 明确确认空闲后恢复（需续跑检查点时配合 `--resume`）。

3. **沿用多端口脚本而非 `mc-agent-api >= 1.3.13` 单端口多 Bot 架构**
   - **现象**：过去为每个 Bot 启动 `18901..18909` 一排端口既繁琐又不符合新版多 Bot 进程隔离规范。`mc-agent-api >= 1.3.13`（当前基线 `1.3.19`）已在单端口（默认 `http://127.0.0.1:18900/api/v1`）内按 Bot 隔离运行实例。
   - **规避**：多 Bot 统一使用 `scripts/multi_bot_runner.py`（支持 `--start-api`、`--spread`、`--dry-run`），指向同一 `mc_api.base_url` 并为每个 Bot 分配独立的 `state_dir`、`world_id`、`session_file`、日志目录与只读监控端口（`18931+`）；错峰登录会自动处理 `429 rate_limited`（`retry_after_s`）与 `503 routing_unavailable`。

4. **多 Bot 模型门禁争用（`llm_busy`）与飞行日志速查**
   - **现象**：多进程同时请求同一个模型服务时，受 `ModelRequestGate` 串行保护，后发进程会收到 `llm_busy`。当前 harness 已将其与真正故障分离（不累加 `failures`，仅做 `1.0..2.5s` 带用户名抖动的短退避），不应强行并发绕过门禁。
   - **日志速查**：排查 `logs/flight_*.jsonl` 时重点检索：
     - `LLM_REQUEST`：查看 `context_bytes` 与 `history_turns` 是否逼近限额（`starter` 为 30000B，`competition` 为 48000B）；
     - `LLM_HISTORY_COMPACTED`：确认是否因上下文过大连续丢弃历史；
     - `MODEL_REQUEST_UNCERTAIN`：定位超时或断连瞬间（`elapsed_ms`），按第 2 条人工确认空闲后再解锁；
     - `SAFETY_OVERRIDE`：排查是否因夜间无床或门禁阻塞导致停滞。

## 默认离线回归

```powershell
$env:BOTSLEEP_RUN_LIVE_TESTS = "0"
python -B -m unittest discover -s tests -v
```

HTTP 用例使用本机临时假服务。门户三步实服 unittest 默认跳过，不能记为通过；合成和随机矩阵文件只有 main 脚本，不是离线 unittest。`test_live_test_isolation.py` 在子进程禁止网络和 RCON 导入，验证三个入口的关闭状态。

## 显式实服测试

这些脚本会清空物品、给物、传送、改变模式/效果或填充方块，只能在已授权的专用测试世界运行；本轮未执行其主体。默认生产依赖不包含 mcrcon，先安装 `requirements-dev.txt`。

需显式设置 `BOTSLEEP_RUN_LIVE_TESTS=1`，并由本机环境提供 `MC_AGENT_API`、`MC_AGENT_PASSWORD`、`MC_RCON_PASSWORD`。可选 `MC_RCON_HOST`（默认回环地址）、`MC_RCON_PORT`（默认 25575）；固定坐标命令针对 SleepBot，若设置 `MC_AGENT_USER` 必须为 SleepBot。不要把真实凭据写入仓库或文档。

```powershell
# 仅在配置好专用测试服务器和上述环境变量后运行
$env:BOTSLEEP_RUN_LIVE_TESTS = "1"
python -B -m unittest discover -s tests -p test_speedrun_portal.py -v
python -B tests/test_crafting.py
python -B tests/test_random_lava_matrix.py --trials 20 --start 1
```

门户脚本验证分步放石/放水、底部浇筑和后续点火；第一步靶场会给抗火效果，不能当作纯自然生存验收。随机岩浆矩阵通过 RCON 生成有噪声岸线、树木和接近路线，零伤害、零溢水和至少 90% 成功率是目标，不能由文件存在或离线跳过推断达成。独立合成脚本清包给 8 根原木后尝试制作木镐；物品前置由靶场提供，不代表自然采集完整链通过。

完成开发后关闭显式实服开关，再运行默认离线命令。自动搭高、落地水及 20Hz ReflexDaemon 仍未接入，不能把传送至高空的场景描述当作已可用测试工具。新增能力的真实 Python 参数见 [SKILL_SPEC.md](SKILL_SPEC.md)。

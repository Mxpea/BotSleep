# 调试、日志与实服靶场

更新：2026-11-15，源码基线 `3801be9`。普通运行方式见 [README.md](../README.md)，本轮结果及历史证据见 [TESTING.md](TESTING.md)。

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

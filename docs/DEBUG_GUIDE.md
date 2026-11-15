# 调试与验证

## 当前游戏内命令

调试通过配置 `debug.enable_in_game_commands: true` 或命令行 `--debug` 开启。必须在 `debug.allowed_usernames` 中列出允许控制 Bot 的玩家，空列表不接受聊天指令。

支持 API `messagestr` 中的标准 `<Player> #command` 格式。不执行匿名系统消息、机器人自己的消息或未列入名单的玩家指令。服务器插件自定义的聊天格式尚未适配。

| 指令 | 实际行为 |
|---|---|
| `#help` | 列出已注册命令 |
| `#status` | 睡眠模式查询位置/生命/睡眠；生存模式显示任务、步骤和暂停原因 |
| `#stop` | 设置本地取消信号，请求 API 停止，并暂停后续自动动作 |
| `#sleep` | 恢复自动睡眠并请求一次尝试；已有任务时不会启动第二个 |
| `#resume` | 仅生存模式，解除暂停并重新观察；不重放旧检查点动作 |

`#look`、`#pillar`、`#mlg`、`#llm` 尚未实现，不列入帮助。

技能在后台线程执行，等待天亮不会阻塞聊天轮询。正在等待的同步 HTTP 调用受自身超时限制；停止回执只说明已经请求停止。取消睡眠不会再次点击床，以免起床/入睡竞态。Ctrl+C 会取消任务、等待工作线程结束，然后登出；清理失败不会跳过登出步骤。

## 日志

`debug.flight_recorder.output_dir` 相对配置文件目录。启用后写入 JSONL，时间为带毫秒的 UTC。

当前事件包括启动、睡眠开始、结果、停止请求、崩溃和退出。睡眠结果包含标准 `SkillResult`、尝试床位、错误码和验证证据。

生存模式还记录 `DECISION`、`STEP_RESULT`、`SAFETY_INTERRUPT`、`DEATH_OBSERVED`、`RECOVERY_REQUIRED`、`PAUSED` 和预算耗尽事件。无论飞行日志是否开启，`survival.state_dir` 都保存按服务器/机器人/world_id 隔离的原子检查点和事件记录。HTTP 超时或内部验证异常后的 pending 不会自动重放。

`--resume`（或可信玩家的 `#resume`）只解除生存控制器暂停并重新观察；模型请求的跨进程未知锁独立处理。`--acknowledge-model-idle` 必须先确认 provider 已无请求执行，不得仅凭客户端超时或模型取消就使用。详细错误与恢复契约见 [SURVIVAL.md](SURVIVAL.md) 和 [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md)。

启用 `debug.monitor.enabled` 后，`debug/monitor.py` 提供本机回环地址上的只读网页，默认端口18930。展示任务板、观察与执行证据、模型请求/返回及实际 `response.model`、token/耗时、模型槽状态和记忆摘要。只展示运行时显式发布的事件，经过脱敏和数量/大小限制；不是通用文件浏览器或远程控制台。模型返回的 reasoning/content 字段属于服务返回数据，不代表可以检查模型内部状态。

目前没有全量三维世界快照或完整自动游戏回放。模型原始响应和技能结果日志可用于排查，但不替代实机后验。正式比赛关闭聊天调试；本机只读监控也不能被当作人工代操作入口。

## 离线回归

```powershell
python -B -m unittest discover -s tests -v
```

HTTP 集成用本机临时假服务验证真实请求和主入口生命周期，不连接 Minecraft。

## RCON 开发工具

`core/rcon.py`、`debug/playground.py` 为可选开发工具。普通自动睡眠不导入或使用 mcrcon，仅使用靶场时安装 `requirements-dev.txt`。

靶场会给物品、传送、放床、改时间或生成生物，只能在专用测试世界调用。已有 playground 方法只是场景布置，不能单独作为技能通过验收的证据。

实际游戏测试步骤和结果见 [TESTING.md](TESTING.md)。

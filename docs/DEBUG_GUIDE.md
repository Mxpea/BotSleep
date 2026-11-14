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

目前没有全量三维现场快照、模型推理追踪或自动回放。

## 离线回归

```powershell
python -B -m unittest discover -s tests -v
```

HTTP 集成用本机临时假服务验证真实请求和主入口生命周期，不连接 Minecraft。

## RCON 开发工具

`core/rcon.py`、`debug/playground.py` 为可选开发工具。普通自动睡眠不导入或使用 mcrcon，仅使用靶场时安装 `requirements-dev.txt`。

靶场会给物品、传送、放床、改时间或生成生物，只能在专用测试世界调用。已有 playground 方法只是场景布置，不能单独作为技能通过验收的证据。

实际游戏测试步骤和结果见 [TESTING.md](TESTING.md)。

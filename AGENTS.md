# Repository Guidelines

## 项目结构与模块组织

BotSleep 基于 mc-agent-api 控制 Minecraft 机器人，采用大模型决策与本地技能分层设计。

- `main.py`：模块化入口；`agent/` 负责模型调用、工具定义和决策循环。
- `core/`：HTTP 客户端、RCON、状态感知和记忆管理。
- `skills/`：移动、落地水和睡眠技能；遵循 `skills/__init__.py` 中的 `BaseSkill`、`SkillResult` 契约。
- `debug/`：聊天指令、RCON 靶场和结构化日志；`data/game_logic/` 保存状态、地标与事件记忆。
- `docs/`：架构、技能契约和调试规范。修改相关行为前先阅读对应文档。
- `integrations/ninjabrain/`：无界面要塞计算与坐标采样；`tests/` 为 unittest 回归测试，`data/examples/` 存合成输入。
- 根目录 `bot_sleep.py`、`client.py` 是独立睡眠脚本及其客户端；修改前确认使用的是哪套入口。

## 构建、测试与开发命令

在仓库根目录使用 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install requests mcrcon
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe main.py --debug
.\.venv\Scripts\python.exe bot_sleep.py
```

前两步创建环境并安装当前代码依赖；两个启动命令分别运行模块化入口和独立睡眠脚本，按需选择。运行前需启动 Minecraft 服务及 mc-agent-api，默认接口为 `http://127.0.0.1:18900/api/v1`。

当前没有构建流程或依赖清单。`main.py` 虽接受 `--config`，但尚未读取 YAML；连接参数仍在代码中设置，修改模板不会自动生效。

## 编码风格与命名约定

沿用 Python 四空格缩进；模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。公共接口添加类型注解与简短文档字符串。HTTP 交互集中在客户端，组合动作放入技能层，模型决策放入 `agent/`。技能失败通过 `SkillResult` 返回明确错误码，并处理超时和动作恢复。当前未配置格式化或 lint 工具，避免无关格式改动。

## 测试与验证

使用标准库 `unittest`，目前无覆盖率门槛。先执行 `python -m integrations.ninjabrain setup` 安装计算引擎（需 JDK 11+），再运行测试；缺少 JAR 时引擎用例会跳过。提交前运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall main.py agent core skills debug integrations
```

涉及游戏行为时，在专用测试世界验证登录、状态读取和目标技能，并记录复现步骤、预期结果及日志。新增自动化测试放在 `tests/test_<module>.py`，模拟 HTTP、模型与 RCON 响应。部分技能、决策循环及 `#stop` 仍有 TODO；文档中的设计目标不代表功能已经实现。

## 提交与拉取请求

现有提交包含 `init` 和 `feat: ...`，尚无完整强制规范；建议使用 `feat:`、`fix:`、`docs:` 等前缀，概括单一变更。拉取请求应说明目的、影响模块、验证命令和结果，关联相关 issue；涉及游戏行为时附日志或截图，并同步更新技能契约或架构文档。

## 配置与数据安全

不要提交真实 API 密钥、机器人密码或 RCON 凭据。`config.yaml`、`.env`、`.venv/` 和 `logs/` 已被忽略。`data/game_logic/` 中的数据受版本控制，提交前检查是否混入本地运行状态或私人服务器信息。

计算与采样说明见 `docs/STRONGHOLD.md`。纯计算不连接游戏；`capture` 会消耗末影之眼并登录后登出。生成的 `.cache/ninjabrain/` 不提交，上游许可证见 `third_party/ninjabrain/`。

## Harness AI Skill 编写约定

后续 skill 默认面向小模型：

- 正文简短，以明确目标、调用表和不超过五步的流程为主；默认争取约 300～600 中文字，必要接口示例可适量超出。
- 接口写明真实名称或 HTTP 方法/路径、参数示例、单位及关键返回字段。尚未绑定的入口必须注明，不虚构可调用工具。
- 原理、公式、来源和长示例放入 `references/`，不默认注入模型上下文。只给当前任务需要的状态与结果摘要。
- 会话管理、坐标转换、采样、数学计算及并发监控由 harness 负责；skill 只让模型选择行动，并给出明确的完成、失败和中止条件。

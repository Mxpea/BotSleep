# BotSleep

基于 **mc-agent-api** 的 Minecraft 自动作息管理与生存自律 AI Agent。

采用 **“大模型慢思考 + 本地宏技能快反应 + 双向协同调试网关”** 的分层设计。

---

## 📑 核心文档索引

在进行具体功能编码前，请先查阅以下设计规范与开发文档：

- **[系统架构设计规范 (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md)**：深入阐述大模型认知层、程序化技能层、应急反射环与驱动层的分层设计。
- **[人机协同调试指南 (docs/DEBUG_GUIDE.md)](docs/DEBUG_GUIDE.md)**：包含游戏内 `#` 聊天调试指令、RCON 上帝视角靶场演练及飞行记录仪使用说明。
- **[技能契约与规范 (docs/SKILL_SPEC.md)](docs/SKILL_SPEC.md)**：定义自动搭高 (`pillar_up`)、自动落地水 (`mlg_water`)、智能睡眠 (`auto_sleep`) 等宏动作的输入参数与异常处理。

---

## 📂 模块结构概览

```
BotSleep/
├── docs/                         # 📚 项目架构与调试规范文档
│   ├── ARCHITECTURE.md           # 系统总体架构设计规范
│   ├── DEBUG_GUIDE.md            # 游戏内聊天调试与 RCON 靶场指南
│   └── SKILL_SPEC.md             # 程序化宏技能契约规范
│
├── core/                         # 🔌 驱动与底层基础设施
│   ├── client.py                 # mc-agent-api HTTP REST 接口客户端
│   ├── rcon.py                   # RCON 客户端管理者
│   └── telemetry.py              # 环境感知与 20Hz 应急反射监听环
│
├── skills/                       # 📦 程序化宏技能库 (快反应)
│   ├── movement.py               # 自动搭高 (pillar_up)、搭桥、潜行
│   ├── clutch.py                 # 自动落地水 (mlg_water)
│   └── sleep.py                  # 寻床入睡核心流程 (auto_sleep)
│
├── agent/                        # 🧠 大模型认知层 (慢思考)
│   ├── brain.py                  # OpenAI 兼容接口封装 (GPT/DeepSeek/Qwen/Ollama)
│   ├── tools.py                  # Function Calling 技能工具定义
│   └── loop.py                   # Observe-Think-Act 状态机循环
│
├── debug/                        # 🛠️ 人机协同调试工具箱
│   ├── chat_debugger.py          # 游戏内 # 聊天调试指令分发器
│   ├── playground.py             # RCON 自动化模拟靶场 (测落地水/测搭高)
│   └── logger.py                 # 结构化飞行记录仪 (Flight Recorder)
│
├── .venv/                        # 隔离 Python 虚拟环境
├── config.example.yaml           # 全局配置模板
├── main.py                       # 项目主启动入口
├── .gitignore                    # Git 忽略配置
└── README.md
```

---

## 🚀 快速启动

### 1. 配置参数
复制配置模板并按需修改：
```powershell
cp config.example.yaml config.yaml
```

### 2. 启动 Agent（附带游戏内聊天调试支持）
在当前项目的隔离虚拟环境中运行：
```powershell
.\.venv\Scripts\python.exe main.py --debug
```

### 3. 游戏内交互调试
在 HMCL 游戏内聊天框按 `T` 输入：
- `#help`：查看当前已加载的调试指令
- `#status`：实时回显 Bot 坐标、血量与状态
- `#stop`：紧急打断 Bot 当前动作

---

## 要塞计算（Ninjabrain Bot）

已接入官方 1.5.2 的无界面计算核心，可从投眼坐标射线输出候选区块与概率。先用合成数据离线验证：

```powershell
.\.venv\Scripts\python.exe -m integrations.ninjabrain setup
.\.venv\Scripts\python.exe -m integrations.ninjabrain calculate --input data/examples/stronghold_throws.json
```

真实投眼采样、误差假设和限制见 [要塞计算集成指南](docs/STRONGHOLD.md)。采样会消耗末影之眼；已通过 5 次真实投眼采样与多观测计算实测；尚未挖掘确认传送门房。

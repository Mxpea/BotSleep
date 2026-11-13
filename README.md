# BotSleep

基于 **mc-agent-api** 的 Minecraft 自动作息管理与生存自律 AI Agent。

采用 **“大模型慢思考 + 本地宏技能快反应 + 3D 方块雷达感知 + 双向协同调试网关”** 的分层设计。

---

## 👥 协作分支规范 (Multi-Developer Workflow)

- **`main` 分支**：经过验证的稳定发布主线。
- **`develop-tianya` 分支**：TianYa 的个人专属功能开发与迭代工作分支。
- **存档隔离**：Minecraft 运行世界（`world/`）物理隔离在独立服务端，不混入代码仓库。
- **密钥安全**：本地实际配置保存在 `config.yaml`（已配置 `.gitignore` 绝不入库），示例配置见 `config.example.yaml`。

---

## 📑 核心文档索引

在进行开发前，请先查阅以下设计规范与开发文档：

- **[系统架构设计规范 (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md)**：深入阐述大模型认知层、Sub-Agent 派发、3D 方块雷达与空间知识库。
- **[人机协同调试指南 (docs/DEBUG_GUIDE.md)](docs/DEBUG_GUIDE.md)**：包含游戏内 `#` 聊天指令（`#radar on/scan/poi` 等）、RCON 上帝视角靶场演练及飞行记录仪说明。
- **[技能契约与规范 (docs/SKILL_SPEC.md)](docs/SKILL_SPEC.md)**：定义常态化雷达勘探 (`ContinuousRadarSkill`)、自动搭高 (`pillar_up`)、自动落地水 (`mlg_water`)、智能睡眠 (`auto_sleep`) 等宏动作。

---

## 📂 模块结构概览

```
BotSleep/
├── docs/                         # 📚 项目架构与调试规范文档
│   ├── ARCHITECTURE.md           # 系统总体架构设计规范
│   ├── DEBUG_GUIDE.md            # 游戏内聊天调试与 RCON 靶场指南
│   └── SKILL_SPEC.md             # 程序化宏技能契约规范
│
├── core/                         # 🔌 基础设施与驱动
│   ├── client.py                 # mc-agent-api HTTP REST 接口客户端
│   ├── recipes.py                # 配方管理器与多层级原料预算规划器 (RecipeBook)
│   ├── radar.py                  # 3D 方块雷达与地形高度图引擎 (BlockRadar)
│   ├── memory.py                 # 空间记忆与 Sub-Agent 知识库 (MemoryEngine)
│   ├── rcon.py                   # RCON 客户端管理者
│   └── telemetry.py              # 环境感知与 20Hz 应急反射监听环
│
├── skills/                       # 📦 程序化宏技能库 (快反应)
│   ├── crafting.py               # 智能合成技能 (CraftItemSkill: 2x2/3x3 自适应/自动放回收工作台)
│   ├── mining.py                 # 智能伐木与清理整树 (ChopTreeSkill)
│   ├── exploration.py            # 常态化雷达自动勘探技能 (ContinuousRadarSkill)
│   ├── movement.py               # 自动搭高 (pillar_up)、搭桥、潜行
│   ├── clutch.py                 # 自动落地水 (mlg_water)
│   └── sleep.py                  # 寻床入睡核心流程 (auto_sleep)
│
├── agent/                        # 🧠 大模型认知层 (慢思考)
│   ├── brain.py                  # OpenAI 兼容接口封装 (支持任何兼容端点)
│   ├── tools.py                  # Function Calling 技能工具定义 (含合成、雷达与记忆检索)
│   └── loop.py                   # Observe-Think-Act 状态机循环
│
├── data/                         # 💾 游戏逻辑数据与空间知识库
│   ├── recipes.json              # Minecraft 1.21.11 完整 886 种配方库
│   └── game_logic/
│       ├── bot_state.json        # Bot 坐标与生命体征实时快照
│       ├── landmarks.json        # 战略 POI 数据库 (床/箱子/工作台/矿物/液体)
│       ├── spatial_grid.json     # 已探索区块与航迹
│       ├── world_context.json    # 生态与昼夜环境上下文
│       └── episodic_memory.jsonl # 探索时序回忆流水日志
│
├── tests/                        # 🧪 自动化场景验收测试集
│   └── test_crafting.py          # 智能逆向合成木镐全链路实机验收测试
│
├── debug/                        # 🛠️ 人机协同调试工具箱
│   ├── chat_debugger.py          # 游戏内 # 聊天调试指令分发器 (#radar, #status 等)
│   ├── playground.py             # RCON 自动化模拟靶场 (测落地水/测搭高)
│   └── logger.py                 # 结构化飞行记录仪 (Flight Recorder)
│
├── .venv/                        # 隔离 Python 虚拟环境
├── config.example.yaml           # 全局配置模板
├── main.py                       # 项目主启动入口 (支持 --radar, --debug)
├── .gitignore                    # Git 忽略配置
└── README.md
```

---

## 🚀 快速启动

### 1. 启动 Agent 并开启常态雷达勘探
在当前项目的隔离虚拟环境中运行：
```powershell
.\.venv\Scripts\python.exe main.py --radar --debug
```

### 2. 游戏内交互调试
在 HMCL 游戏内聊天框按 `T` 输入：
- `#help`：查看当前已加载的调试指令
- `#status`：实时回显 Bot 坐标、血量与雷达状态
- `#radar on` / `#radar off`：实时开关常态自动化雷达勘探
- `#radar scan`：强制执行一次 3D 全景雷达测绘
- `#radar poi`：查询距离最近的 4 个关键战略兴趣点

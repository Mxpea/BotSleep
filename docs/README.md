# BotSleep 文档总目录

> **当前主分支（`main`）运行的是 BotSleep v2（L0–L4 行为树微内核架构）**。旧版 v1 代码已永久归档于 Git 标签 `archive/v1-final`（`c6859dc`）。阅读任何文档前请先对照下表确认其所属版本，避免误用旧路径或旧命令。

---

## 一、 现行文档（BotSleep v2 / `main` 分支）

| 文档 | 适用范围 | 说明 |
|---|---|---|
| [README.md](../README.md) | **首读入口** | 安装、跨机环境一致性清单、`tests_v2` 验证命令、运行模式与升级说明 |
| [COMPETITION_DEPLOY.md](COMPETITION_DEPLOY.md) | **8h 单 Bot 模拟赛部署（分支专题）** | `developer-tianya/competition-task-advancements` 阶段成果、已知边界与新机单 Bot + 仅主 API 8h 部署手册 |
| [AGENTS.md](../AGENTS.md) | 协作者 / AI 规则 | 模块职责边界、编码与测试纪律、`mc-agent-api` 零修改约束 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架构设计 | L0–L4 单向五层执行链、核心不变量与分支协作规范 |
| [CONFIGURATION.md](CONFIGURATION.md) | 配置说明 | `v2/kernel/config.py` 参数表、环境变量覆盖顺序与 `overflow` 双路配置 |
| [TESTING.md](TESTING.md) | 测试账本 | 最新 v2 离线覆盖与实服压测记录（注：第 50 行起为历史 v1 对照记录） |
| [LIVE9_90M_REPORT_20261126.md](LIVE9_90M_REPORT_20261126.md) | 最新实服报告 | 9-Bot 90 分钟双 API 压测、`single_r4_15m` 验证与 `80.81%` 代码覆盖 |
| [OVERNIGHT_TEST_REPORT_20261126.md](OVERNIGHT_TEST_REPORT_20261126.md) | 实服专题报告 | R1–R3 及过夜 9-Bot 30 分钟实服测试明细 |
| [API_AND_TOKEN_REPORT_20261126.md](API_AND_TOKEN_REPORT_20261126.md) | Token 与接口报告 | `LLM_HTTP` Token 账单与 `mc-agent-api` 端点统计 |
| [REWRITE_V2_MASTERPLAN.md](REWRITE_V2_MASTERPLAN.md) | v2 总方案 | L0–L4 重写设计总纲与 10 项工程修正 |
| [REWRITE_V2_AGENT_PLAN.md](REWRITE_V2_AGENT_PLAN.md) | v2 实施路线 | P0–P6 分阶段实施计划与验收清单 |
| [STRONGHOLD.md](STRONGHOLD.md) | 要塞计算模块 | `integrations/ninjabrain/` 无界面 Ninjabrain Bot 安装、离线计算与实机采样 |
| [data/competition/README.md](../data/competition/README.md) | 赛事规则目录 | `rules_v2.json` 规则快照与 `v2.tasks.competition.rules` 接口 |
| [skills_kb/README.md](../skills_kb/README.md) | 微技能知识库 | 16 张 L4 BM25 召回微技能卡说明 |

---

## 二、 历史归档文档（BotSleep v1，对应 `archive/v1-final` @ `c6859dc`）

以下文档均已在顶部添加 **`[!WARNING]` 历史归档提示**，仅用于回溯旧版设计背景与思路，文中的 `core/`、`agent/`、`skills/`、`debug/`、`tests/` 路径和旧测试命令**在当前 `main` 分支已失效**：

| 归档文档 | 原记录内容 |
|---|---|
| [HANDOFF.md](../HANDOFF.md) / [docs/HANDOFF.md](HANDOFF.md) | 旧版 v1 交接与 v24–v31 历史基准 |
| [AUTONOMOUS_TASKS.md](AUTONOMOUS_TASKS.md) | v1 `AutonomousTaskController` 有界目标程序 |
| [BUILD_PROJECTION.md](BUILD_PROJECTION.md) | v1 程序建筑投影设计 |
| [CAMPAIGN.md](CAMPAIGN.md) | v1 速通通关流程草案 |
| [COMBAT_AND_HUNTING.md](COMBAT_AND_HUNTING.md) | v1 战斗、狩猎与桶操作说明 |
| [COMBAT_AND_LOOT_DESIGN.md](COMBAT_AND_LOOT_DESIGN.md) | v1 自保与掉落拾取设计 |
| [COMBAT_LIVE_TEST.md](COMBAT_LIVE_TEST.md) | v1 自保与随行拾取实服手册 |
| [DAILY_SURVIVAL.md](DAILY_SURVIVAL.md) | v1 日常生存策略与旧版记忆说明 |
| [DEBUG_GUIDE.md](DEBUG_GUIDE.md) | v1 调试与 RCON 靶场指引 |
| [HARNESS_INTERFACES.md](HARNESS_INTERFACES.md) | v1 Harness 接口草案与赛事能力边界 |
| [MONITORING.md](MONITORING.md) | v1 监控面板字段说明（v2 现行实现位于 `v2/observability/monitor.py`，无 `debug.monitor_tail` CLI） |
| [SKILL_SPEC.md](SKILL_SPEC.md) | v1 `skills/` 技能契约规范 |
| [SURVIVAL.md](SURVIVAL.md) | v1 生存与 Harness 运行指南 |
| [SURVIVAL_MAINLINE.md](SURVIVAL_MAINLINE.md) | v1 15 章无龙生存主线 |
| [SURVIVAL_PLAYBOOK.md](SURVIVAL_PLAYBOOK.md) | v1 `SurvivalPlaybook` 阶段建议与未绑定边界 |

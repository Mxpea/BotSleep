# 赛事规则目录（草案 v2）

`rules_v2.json` 逐行提取 `mc-agent-api/docs/RULES.md` 的全部 120 项计分表记录，
并补入原文规定的 5 个零分根节点。分类数量为 story 15、nether 22、end 8、
adventure 46、husbandry 29，另有根节点 5 项。

目录是纯数据，不承诺现有工具能够执行所有 advancement，也不确认成就完成或裁判得分。
外部 API 和源规则仓库无需任何修改；运行和离线测试不依赖源文件仍在本机。

## 数据与来源

- `version: v2`、`status: draft` 保留草案身份；`schema_version: 1` 是目录格式版本。
- `date: 2026-11-15` 是目录提取日期，`date_kind: catalog_snapshot`。
  原文没有发布日期，故 `source.date: null`，不推断赛事发布日期。
- `source.document` 为可移植相对标识，`source.sha256` 是源文件原始字节的 SHA256：
  `e896244ae7e3f15a816d1aedeab30478035e32eee6758f1c97cca2da43cca0b9`。
- `condition`、`title`、`difficulty` 保留原表文本/等级；`source_points` 保留原表分值字符串，
  包括不计分标记 `—`。不计分项及根节点的数值 `points` 为 0，其余按难度 1..5
  映射 5/15/30/60/100；根节点 `difficulty: 0`、`source_points: "0"`。
- `classification` 是原始分类：`unconditional`（45）、`conditional`（54）、
  `excluded`（21 个不计分项及 5 个根节点）；`is_root` 标记根节点。
- `minecraft:story/follow_ender_eye` 原文条件是“岛上有下界要塞时计分”，照录保留。

## 给 runtime 的 Python 接口（BotSleep v2）

```python
from v2.tasks.competition.rules import load_competition_rules, scoring_catalog

entries = load_competition_rules()            # list[CompetitionEntry]，只读加载默认 rules_v2.json
eligible = scoring_catalog({
    "minecraft:story/follow_ender_eye": True,
})                                            # dict[str, CompetitionEntry]，仅含可计分项
```

精确签名（`v2/tasks/competition/rules.py`）：

- `load_competition_rules(path: Optional[Path] = None) -> List[CompetitionEntry]`
- `scoring_catalog(eligibility_overrides: Optional[Mapping[str, bool]] = None, *, path: Optional[Path] = None) -> Dict[str, CompetitionEntry]`

`load_competition_rules` 读取目录全部 125 项为 `CompetitionEntry`（含 `id / title / category / difficulty / points / classification / condition / is_root`）。`scoring_catalog` 仅返回当前可计分条目：45 项 `unconditional` 默认启用；54 项 `conditional` 必须由 `eligibility_overrides` 显式设为 `True` 才启用；26 项 `excluded`（21 个不计分项 + 5 个根节点）永不纳入。向 `eligibility_overrides` 传入未知 ID、非布尔值或非条件项均抛出 `ValueError`。

模型发现结构、地形或自述进度不能代替服务端 `/advancements` 官方计分真值；本模块纯只读，无网络 I/O、无全局可变缓存。

## 离线验证（BotSleep v2）

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests_v2 -v
```

`tests_v2/tasks/test_tasks_l3.py` 校验 `45` 无条件 + `54` 条件 override 门禁、禁用项拒绝及服务端 `/advancements` 真值计分；测试不连接真实游戏或大模型。

---

> [!NOTE]
> **以下为 2026-11-15 目录引入时的历史 v1 原始提取复核记录**（当时依赖旧路径 `core/competition_rules.py` 与 `tests/test_competition_rules.py`，现已统一迁移至上方 v2 `v2/tasks/competition/rules.py` 与 `tests_v2`）：

本轮复核（2026-11-15，Windows，`.venv` Python 3.11.3）：源码基线
`c21d62365c3ee02cce380146344e79e2187b8f4e` 加本目录/模块/测试新增文件。
本轮只读对比外部源规则，120 行全部字段一致，源字节 SHA256 一致。

- 新增规则测试共 17 项，在全量运行中全部通过，无跳过。
- 原 v1 全量单测（已归档至 `archive/v1-final`）：
  共 789 项，19.255 秒，788 项通过、1 项 ERROR、0 跳过，非全绿。
- 唯一错误为既有 `test_survival_memory.SurvivalMemoryTests.test_corrupt_and_future_schema_databases_are_preserved`：
  清理临时 SQLite 文件时出现 WinError32，随后 WinError267；与 `docs/TESTING.md`
  已记录的错误相同。本轮未修改该模块，未证实或解决文件占用根因。
- `git diff --check` 通过。未连接真实游戏、赛事 API 或模型。

本任务写集仅允许本目录、`core/competition_rules.py` 和
`tests/test_competition_rules.py`，故此处保存本轮验证结果，供主线程集成后汇入
`docs/TESTING.md`；历史结果不能替代本轮验证。

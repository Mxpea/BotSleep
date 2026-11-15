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

## 给 runtime 的 Python 接口

```python
from core.competition_rules import load_rules, scoring_catalog

catalog = load_rules()                 # dict，包含 entries 及来源元数据
catalog = load_rules("rules.json")     # 可指定 str 或 pathlib.Path，只读加载并校验
rows = scoring_catalog({
    "minecraft:story/follow_ender_eye": True,
    "minecraft:adventure/sleep_in_bed": False,
})
tasks = [row for row in rows if row["eligibility"] == "scored"]
```

精确签名：

- `load_rules(path: str | Path | None = None) -> dict`
- `scoring_catalog(overrides: Mapping[str, bool] | None = None) -> list[dict]`

`scoring_catalog` 始终从默认内置目录读取，返回全部 125 项的新字典，至少包含
`id/title/difficulty/points/condition/eligibility`，另保留全部原始字段。
`eligibility` 仅有 `scored`、`pending`、`excluded`：无条件项默认 scored；
条件项默认 pending；不计分项及根节点永远 excluded。runtime 仅将 scored 行作为得分任务。

`overrides` 是由 runtime 从配置显式传入的“完整 advancement ID → 布尔值”映射；
本模块不读取配置、不持久化修改。True 确认计分，False 明确禁用；遗漏的条件项仍 pending。
禁用后保留原始分值及条件，runtime 必须通过 eligibility 过滤，不能仅按 points 求和。
启用原文禁分项或根节点、未知 ID、非布尔 override 均抛出 ValueError。
模型发现结构、地形或完成 advancement 不能自动代替官方计分确认；本模块没有观察输入。

两个接口均不写文件、不调用游戏/模型、无全局可变缓存；结果全部可 JSON 序列化。
读文件失败、无效 JSON、非法等级/分值、重复 ID、无效来源及分类等校验错误均为 ValueError。
每次调用返回独立结果，调用方修改结果不会污染后续读取。

## 离线验证

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_competition_rules.py -v
```

测试使用独立完整 ID 清单及原表行内容 SHA256，覆盖所有分类、所有原始标题/等级/分值/条件、
来源、根节点、全部条件项显式开关、全部禁分项不可启用、非法数据拒绝与只读行为。
测试不连接游戏，也不代表技能成功或完整生存目标完成。

本轮复核（2026-11-15，Windows，`.venv` Python 3.11.3）：源码基线
`c21d62365c3ee02cce380146344e79e2187b8f4e` 加本目录/模块/测试新增文件。
本轮只读对比外部源规则，120 行全部字段一致，源字节 SHA256 一致。

- 新增规则测试共 17 项，在全量运行中全部通过，无跳过。
- `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`：
  共 789 项，19.255 秒，788 项通过、1 项 ERROR、0 跳过，非全绿。
- 唯一错误为既有 `test_survival_memory.SurvivalMemoryTests.test_corrupt_and_future_schema_databases_are_preserved`：
  清理临时 SQLite 文件时出现 WinError32，随后 WinError267；与 `docs/TESTING.md`
  已记录的错误相同。本轮未修改该模块，未证实或解决文件占用根因。
- `git diff --check` 通过。未连接真实游戏、赛事 API 或模型。

本任务写集仅允许本目录、`core/competition_rules.py` 和
`tests/test_competition_rules.py`，故此处保存本轮验证结果，供主线程集成后汇入
`docs/TESTING.md`；历史结果不能替代本轮验证。

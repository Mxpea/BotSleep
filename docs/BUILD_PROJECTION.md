# 程序建筑投影

在通关模式里设置 `survival.projection: timber_house`。预设保存在 `data/projections`。程序在生存模式按坐标扫描现有地形，登记已经符合投影的方块，按阶段计算库存差额。备料复用本地采集、制作和熔炼任务。监控的「建筑投影」显示已核验格数、施工阶段和剩余材料。

预设为版本 1 的 JSON。每条规则用 `at` 描述单格，或用 `from` 与 `to` 描述含边界的立方区域。 `except` 是区域中要留空的位置。坐标相对于首层地板中心，`+Z` 是正面，投影可由程序转为四个朝向。阶段顺序由文件指定，每阶段内优先施工较低层。门只登记底格，放置后核验上下半格。

```json
{
  "version": 1,
  "stages": [
    {"name": "floor", "blocks": [
      {"item": "cobblestone", "from": [-2, 0, -2], "to": [2, 0, 2]}
    ]}
  ]
}
```

`timber_house` 是 11×9 的木石主体。它有原木框架、32 格玻璃、可走的内楼梯与阁楼、宽檐山墙、四层收拢的双坡屋顶、石板效果的抬高门阶和有顶门廊。蓝图把靠外的屋顶条带排在内侧前面，屋面完成后再封山墙中段，让机器人保留施工视线和站位。门最后安装。

投影中的原木、木板和木门按同一木种施工，JSON 中的具体木种用于表达这三个用途。空地按背包和附近观察到的树选择，优先使用能备齐整座投影的库存。有木制方块已经放在对应位置时，沿用实际读到的木种。检查点保存选择，同一项目补料和续建均按该木种。监控显示当前锁定的木种。现有自动备料覆盖橡木、云杉、白桦、丛林木、金合欢、深色橡木、红树、樱花和苍白橡木。

当前选址使用有支撑的空地。在已核验站位最多连续放置五块，并逐块读取放置结果和背包扣减。需要换位时找有支撑、净空和视线的候选。旧方块与图纸冲突时记录受阻位置，底部缺少支撑时重新找空地。最终按原坐标分批重新核验整个投影。检查点绑定蓝图内容、坐标、朝向和维度；若重置蓝图，同一处也要重新勘测。

投影与世界绑定及缺料计算参考了 Litematica，分层施工参考了 Baritone。本模块使用 BotSleep 的 Python 代码和原有生存动作接口。 [Litematica 的投影与世界比对](https://github.com/maruohon/litematica/blob/ornithe/1.12.2/src/main/java/litematica/schematic/placement/SchematicPlacement.java) · [Litematica 的材料清单](https://github.com/maruohon/litematica/blob/ornithe/1.12.2/src/main/java/litematica/materials/MaterialListSchematic.java) · [Baritone 的分层建造](https://github.com/cabaletta/baritone/blob/1.21.4/src/main/java/baritone/process/BuilderProcess.java)

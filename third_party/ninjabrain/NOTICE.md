# Ninjabrain Bot 计算核心

- 上游：[Ninjabrain1/Ninjabrain-Bot](https://github.com/Ninjabrain1/Ninjabrain-Bot)
- 固定版本：官方发行版 `1.5.2`，源码标签 [1.5.2](https://github.com/Ninjabrain1/Ninjabrain-Bot/tree/1.5.2)。
- JAR SHA-256：`98099f6321a90d4acec13400e86d05f7af8d60e5639c2f38427e968dc71450ff`，来自 GitHub 发行附件的 digest。
- 许可证：GNU GPL v3，全文见本目录 `LICENSE`；上游贡献者保留其版权。

`setup` 将未经修改的官方 JAR 下载到 `.cache/ninjabrain/`，该目录不提交。桥接只调用其 `Posterior`、要塞区块、生成环、概率与最近要塞约束，不调用上游 `Main`、窗口、剪贴板、热键或 OBS。

`integrations/ninjabrain/CalculatorBridge.java` 是本项目新增的适配源码，按 GPL-3.0-only 提供。其坐标观测类跳过针对人工准星的角度修正，使用每次观测独立的角度标准差；没有修改上游算法。Python 部分以子进程 JSON 协议调用桥接。分发上游 JAR 或适配器时应保留许可证及对应源码获取信息；本文件不将整个既有仓库重新授权。

本次仅接入主世界 Java 1.19+ 的投眼要塞推断，不接入化石 divine、下界盲穿评估、船眼或视觉误差校准界面。

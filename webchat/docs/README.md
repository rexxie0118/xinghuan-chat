# Docs publishing rules

每个迭代必须新增两样东西，且不删除历史：

1. `iterations/<date>-<nn>.html`：迭代记录 + 当次 todo 快照；
2. `architecture/<date>-<name>.svg`：架构图。

然后更新 `manifest.json`：

- 新增 iteration 条目，指向记录和本次架构图；
- 新增 architecture 条目；
- 更新 `updated_at`。

约束：

- 旧架构图只追加，不覆盖；
- 记录里写清楚「已做 / 未做 / 风险 / 下一步」；
- 架构图只画当前真实状态，计划和现状用不同颜色或标注区分；
- docs 页面通过 `/docs/` 访问。

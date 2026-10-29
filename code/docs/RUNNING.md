# 运行指南

正式批量实验统一使用 Campaign。它负责阵容编译、并行补位、暂停恢复和结果验证；
`run-rule`、`run-lineup` 是单个市场的诊断入口，复用相同 Coordinator。

## 1. 配置与检查

先按 [README](../README.md) 安装依赖。已有本地配置不要覆盖：

```text
.ceo-arena/campaigns/retail-leave-one-out-v1.toml
```

新配置可从 [示例](../examples/campaign.example.toml) 复制。一个 `[[api]]` 对应
一个 Key 和它负责的模型；使用精确模型 ID。Base URL 以 `/v1` 结尾，不使用 `/keys`。
Rule 自动加入，不要作为 LLM 写入模型列表。

新实验使用唯一 `campaign_id` 和新的输出目录；同一个实验恢复时二者保持不变。
模型、seed、经济参数、预算和代码指纹在计划冻结后不能静默改变。

生成计划不会调用付费模型：

```powershell
uv run ceo-arena campaign plan `
  --spec .ceo-arena/campaigns/retail-leave-one-out-v1.toml `
  --output .tmp/formal-plan.json
```

核对正式设计：8 个 LLM + Rule，8 家公司，9 个阵容 × 3 seeds = 27 Runs，
每 Run 500 天。模型名单见 [FORMAL_MODEL_PANEL](FORMAL_MODEL_PANEL.md)。

## 2. 启动实验

看板方式：

```powershell
uv run ceo-arena dashboard --port 8766
```

打开 [本地看板](http://127.0.0.1:8766/)，选择 TOML 配置后点击运行。
启动看板不产生模型费用；运行按钮直接启动付费实验，不再额外确认。

CLI 方式与看板使用同一个 Campaign Application：

```powershell
uv run ceo-arena campaign run `
  --spec .ceo-arena/campaigns/retail-leave-one-out-v1.toml `
  --output runs/formal-eight-models-20260911
```

最多 30 Runs；优先配置中靠前的 seed，空位由后续符合条件的 Run 补入。
30 个槽位足以同时运行这份设计的全部 27 Runs；seed 优先表示启动顺序，不是逐 seed 等待。
不同市场的经济、记忆和用量隔离。不要重复启动同一目录的第二个写入者。

## 3. 暂停、检查与恢复

在看板中，运行中的任务按钮用于暂停；已停止／可恢复失败的任务用于继续。
查看状态不需要 API Key：

```powershell
uv run ceo-arena campaign status --output runs/formal-eight-models-20260911
```

使用相同配置、相同源码、相同输出目录恢复：

```powershell
uv run ceo-arena campaign resume `
  --spec .ceo-arena/campaigns/retail-leave-one-out-v1.toml `
  --output runs/formal-eight-models-20260911
```

自动恢复只处理符合完整性要求的状态。源码／计划不匹配、损坏数据等错误不能强行跳过。
已接收决策不重复请求；未完成公司的会话、草稿和已知用量延续，不获得新的窗口预算。
看板关闭不等于 Runner 已停止；以进程和持久化状态为准。

修复 0.15.1 的截断续写／重试卡死时，可在经济规则不变的前提下进行一次显式
[断点升级](RECOVERY_PATCH.md)，保留原 Run 和已完成天数。不能直接替换源码后
用旧哈希启动，也不能删除 allowance 来重置额度。

## 4. 理解数据文件

| 数据 | 用途 |
|---|---|
| Campaign 的 `campaign.sqlite3` | Run 状态、租约、断点、决策回执、验证结果的恢复依据 |
| 单 Run 的 `manifest.json` | 固定配置及源码身份 |
| 单独运行时的 `checkpoint.json` + `decisions.sqlite3` | 世界快照与尚未并入快照的决策回执 |
| `trace.jsonl`、`agent-traces/` | 经济执行与 Agent 审计轨迹 |
| `agent-memory/`、`allowances/` | 私有文件、会话恢复与已报告用量；不是缓存 |
| `performance*.json*`、`scheduler*.json*` | 性能诊断，不是经济状态依据 |
| 结果 JSON、论文导出 | 经过验证后的派生产物，不覆盖原始轨迹 |

不要只复制数据库主文件而遗漏 WAL／SHM；不要删除记忆、用量和回执来“重置”恢复状态。
不同运行方式的文件职责不同，不应把所有 JSON 当成冗余副本。

## 5. 无付费模型的诊断

```powershell
uv run ceo-arena run-rule --companies 8 --days 14 --seed 42 `
  --output runs/diagnostics/rule-check --fresh
uv run ceo-arena replay runs/diagnostics/rule-check
```

`replay` 验证当前版本的单 Run，不调用 LLM；Campaign 完成时自行从数据库验证回放。
`run-lineup --help` 提供单阵容诊断参数；正式多 Key 批量实验仍使用 TOML + Campaign。

天数长时间不动时，先看当前公司决策进度、Provider 错误和 performance 记录，
再检查进程及网络。不要先删锁、改 checkpoint 或换代码；这些可能破坏可恢复性。

## 6. 正式实验与历史实验分开

源码整理也会改变指纹。新正式实验用新输出目录和整理后的冻结计划；
旧预实验继续使用其原始快照，不修改旧 manifest 来绕过一致性检查。
历史数据位置及恢复约束见 [HISTORY](HISTORY.md)。

移除 Fable 后使用 `retail-eight-models-20260911`，在新的输出目录从 Day 0 开始。
旧 30 Runs 保持暂停且保留全部轨迹；不能把九公司世界的断点续成八公司世界。

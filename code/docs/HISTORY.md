# 历史资料与证据来源

本页区分历史验证与当前实现。旧资料不改写版本、Score 或测试数字；
它们不证明当前正式模型面板已经完成实验。

## 本地归档

2026-09-11 整理归档位于项目根目录的 `archive/2026-09-11/`：

- `INDEX.md`：查找、保护目录与恢复说明。
- `baseline/`：整理前的完整现存源码、文档和交付件基准。
- `baseline-files.json`：基准逐文件 SHA-256。
- `historical/`：按原相对路径保留的历史运行、快照、脚本与临时产物。
- `archive-manifest.json`：已归档目录的路径映射、文件哈希和 Junction 目标。
- `validation/`：本次不调用付费模型的验证记录。

归档不进入 Git；其内容是本地实验资料，不是安装包的一部分。
仍被速度筛选／看板使用的目录保留原位，详见 [MAINTENANCE](MAINTENANCE.md)。

## 历史文档对照

下列文档已退出当前阅读路径，原文保存在本地 `baseline/docs/`：

| 历史文件 | 应如何理解 |
|---|---|
| `NEXT_VERSION.md` | 已实施的经济 v3 改动设计，不是下一版待办 |
| `IMPLEMENTATION_RECEIPT.md` | `da98aac` 源修订的交付记录 |
| `ECONOMY_VALIDATION.md` | 经济 v3 + 当时 Rule v1 的程序验证 |
| `INTEGRATION_V3.md` | 历史集成与当时 Rule v2 的验收记录 |
| `PERFORMANCE_V4.md` | 早期无限回复配置的性能修订，不是当前预算 |
| `CALIBRATION.md` 的历史段落 | 旧 v2 参数，不是当前 v3 参数 |

早期提交 `d1c8377`、`da98aac`、`c905756` 也保留各自的历史来源。
当前说明分别位于 ECONOMY、CALIBRATION、RULE_BASELINE、COST_CONTROL、MEMORY 和 EVALUATION。

## 继续保留的论文证据

`docs/validation/` 与 `paper/research/` 中的来源哈希、Rule 参数搜索摘要和
明确标注版本的诊断记录继续纳入版本管理。原始试验目录归档，不删除论文依据。
这些是校准或程序验证，不是正式 LLM 排名；正式数字仅由当前完整实验输出。

## 读取与恢复旧实验

新版不提供旧 Arena 批量入口、旧字段转换或自动数据库迁移。
保存资料不等于保证任意老版本能在新代码下继续运行。
需要恢复时，使用对应冻结源码及依赖，先核对路径、版本和哈希；
旧启动脚本可能含绝对路径，不能直接在移动后的目录执行。
归档 Junction 只记录并保留链接，未复制或移动其共享依赖目标。

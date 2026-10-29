# 工作区维护约定

## 2026-09-11：服务器并发容量更新

当前代码的默认 Run 并发和硬上限均为 30，由 `campaign/models.py` 的
`MAX_CONCURRENT_RUNS` 统一定义；该文件纳入源码指纹。正式 TOML 与示例同步为 30。
未改变模型、seed、决策预算或经济机制，也不增加模型并发上限。
改动会改变冻结计划身份；已有 Windows 失败实验、其冻结源码和回执保留原样，
不能直接改旧数据库或用新 TOML 强行续跑。服务器尚未迁入那些断点或真实密钥。
论文源码只同步了并发数字，交付 PDF/Overleaf 包未在这次代码更新中重新生成；
其余既有论文版本对齐问题仍见本文末节。

## 当前主线

- 保留 `ceo_arena` 命名与当前经济、模型、Rule、预算、记忆和评价协议。
- 正式批量运行只走 Campaign；单市场诊断复用 Coordinator。
- 删除旧 Arena 执行器、历史导入和字段转换、兼容别名、闲置自适应控制。
- 保留资源监测、版本拒绝、租约／CAS、决策回执、会话／用量恢复和回放检查。
- 初始企业价值在当前 CompanyState 中必填，Score 不再猜测缺失值。
- Campaign 的生命周期类型集中在 `campaign/records.py`，SQL 事务仍由 `repository.py` 独占。

`evaluation/arena.py` 是当前分析 Module，不是已删除的旧 `arena/` 执行器。
Python 与 TypeScript 草稿分别服务 Rule 和 LLM；数据库与审计导出也有不同职责。
Provider 流格式适配仍是当前模型接入所必需的逻辑，不属于旧实验格式兼容层。

## 目录使用规则

新实验放 `runs/`；私密配置放 `.ceo-arena/`；临时产物放 `.tmp/`；
历史资料放 `archive/<日期>/` 并附路径索引。不要再在根目录建立按日期／版本命名的
pytest、烟测和临时分析目录。

Python 测试默认使用 `.tmp/pytest`，pytest 缓存为 `.tmp/pytest-cache`，
Ruff 缓存为 `.tmp/ruff`。第三方依赖维持工具要求的位置，不随历史数据归档。

## 本次保护目录

整理时速度筛选进程与 8766 看板仍在使用下列资料，因此保留原位：

- `runs/pilots/luna-speed-screen-seed42-20260911/`
- `runs/pilots/memory-v8-gemini64k-seed42-20260910/`
- `.ceo-arena/pilot-snapshots/luna-speed-screen-seed42-20260911/`
- `.ceosbench/pilot-snapshots/memory-v8-gemini64k-seed42-20260910/`
- `.ceosbench/pilots/run_memory_v8_gemini64k_seed42.py`

`.ceo-arena/` 当前配置与控制文件、OpenCode、Worker 依赖、`.venv` 均不移动。
待所有引用它们的进程停止后，才可将剩余历史资料并入归档；不得自动暂停它们来整理目录。

## 验证原则

整理前后以相同 Rule、seed 和 500 天配置比较每日世界指纹、决策和最终分数；
源码／路径指纹应改变，经济结果不应改变。另运行 Python、Worker、类型检查和 CLI／UI
离线检查。归档逐文件验证内容哈希，不改变历史 manifest 或数据库中的配置身份。

验收结果保存在本地归档 `validation/`，完成后见其中的 `cleanup-report.json`。
这些验证不调用真实付费模型，不代替正式实验，也不保证所有模型盈利。

2026-09-11 验收：417 项 Python 测试、113 项 Worker 测试、类型与静态检查通过；
独立环境离线安装、九公司短运行／回放及看板 HTTP 检查通过。九公司 seed 42 的
500 天整理前后对照，其全部窗口决策、每日指纹、终态与 Score 完全一致，回放通过。
41 个经济／Worker／参数／协议文件逐字节不变；预算及 Rule 类定义的 AST 不变。
主归档覆盖 78 个路径、69,123 个文件，原始运行文件未重写；另保留整理前 298 文件基准。

## 整理时发现的既有论文问题

`paper/scripts/check_code_alignment.py` 发现中英文可复现性附录仍声明包 `0.12.0`、
决策 v6 和运行 v3；整理前基准中已经如此，与当前 `0.14.0` / v8 / v4 不一致。
本次目录与代码整理未擅自重写论文研究陈述或重新生成 PDF。工程回归通过不代表
论文已完成实现对齐；正式发布前还需要单独核对预算、记忆和恢复描述。

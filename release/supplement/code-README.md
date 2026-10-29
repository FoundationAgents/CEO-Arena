# CEO Arena

一个共享市场中的多智能体公司经营基准。各 CEO 使用相同工具与 Harness，自主经营公司；
经济引擎统一结算，不使用 LLM 裁判打分。

**第一次阅读：**本页 → [系统结构](docs/ARCHITECTURE.md) →
[经营机制](docs/ECONOMY.md) → [实验设计](docs/EVALUATION.md)。

## 当前实验

- 8 个 LLM + 1 个 Rule 候选，每个市场有 8 家公司。
- 完整 `C(9,8)`：每个 seed 9 个阵容；seeds 为 11、29、47，共 27 Runs。
- 每 Run 经营 500 天，每 7 天进行一次同步决策。
- 最多 30 Runs 并行；优先同 seed，有空位时允许后续 seed 补位。
- 唯一 Score：**期末现金 + 在手／已付款在途库存清算价值 − 初始企业价值**。
- 完整名单见 [正式模型面板](docs/FORMAL_MODEL_PANEL.md)；不以预实验充当正式结果。

当前 Campaign 为 `retail-eight-models-20260911`：移除 Fable，其余模型 Key 和设置不变。
原 30 Runs 保持暂停、记录保留；公司数改变后新开实验，不复用旧断点。

每个 CEO 每窗口最多 **32 次模型回复、131,072 个生成 Token、60 分钟活跃时间**。
总重试次数和模型并发默认不设上限，但恢复不重置已消耗的窗口预算。
详见 [用量控制](docs/COST_CONTROL.md)。

## 安装与运行

在项目根目录执行（Windows PowerShell）：

```powershell
uv sync --extra dev
git submodule update --init OpenCode
Push-Location OpenCode
npx --yes bun@1.3.14 install --frozen-lockfile --ignore-scripts
Pop-Location
uv run ceo-arena setup-worker
```

本地正式配置位于 `.ceo-arena/campaigns/retail-leave-one-out-v1.toml`。
已有配置不要覆盖；新配置从 [示例 TOML](examples/campaign.example.toml) 复制。
不同 API Key 的模型写入各自的 `[[api]]`，Rule 自动加入。

启动看板：

```powershell
uv run ceo-arena dashboard --port 8766
```

打开 [本地看板](http://127.0.0.1:8766/)，选择配置，点击运行。
**运行按钮会调用付费 API**；启动看板或查看计划不会。

CLI 批量运行、暂停／恢复、单阵容诊断及故障排查见 [运行指南](docs/RUNNING.md)。
正式批量实验只使用 Campaign，不再提供旧 Arena 批量执行器或历史格式导入。

## 代码地图

| 目录 | 唯一职责 |
|---|---|
| `src/ceo_arena/campaign/` | 阵容编译、多个 Run 的调度与持久化状态 |
| `src/ceo_arena/runtime/` | 单个 Run 的决策屏障、断点、回放和用量恢复 |
| `src/ceo_arena/economy/` | 市场、公司经营、每日经济结算 |
| `src/ceo_arena/harness/` | Python Rule 与 OpenCode Worker Adapter |
| `agent_worker/src/` | LLM 对话、工具草稿、私有记忆和预算 |
| `src/ceo_arena/evaluation/` | Score 与 Track A/B/C，不调用模型 |
| `src/ceo_arena/dashboard/` | 状态展示及单独的运行控制 Interface |
| `src/ceo_arena/reporting/`、`paper/` | 结果输出、论文源码和图表 |
| `tests/` | 离线单元／集成与回放测试 |
| `configs/`、`examples/` | 经济参数与无密钥运行配置模板 |
| `scripts/` | 可复用的模型筛选、Rule 搜索和经济验证工具 |
| `OpenCode/` | 固定提交的第三方源码，不是本项目的业务代码 |

运行链路：**UI / CLI → Campaign → Coordinator → 各 CEO → MarketWorld**。

只有 `MarketWorld` 修改经济状态。工具暂存草稿，所有公司决策齐备后才统一结算。
Python 和 TypeScript 草稿分别服务 Rule 与 LLM；前置工具校验不代替环境最终校验。

## 专题文档

| 问题 | 文档 |
|---|---|
| 数据结构、Interface、调用顺序 | [Architecture](docs/ARCHITECTURE.md) |
| 经营公式、经济系数 | [Economy](docs/ECONOMY.md)、[Calibration](docs/CALIBRATION.md) |
| CEO 可以调用什么工具 | [Agent protocol](docs/AGENT_PROTOCOL.md) |
| 如何限制费用、保存记忆 | [Cost control](docs/COST_CONTROL.md)、[Memory](docs/MEMORY.md) |
| Rule 规则与参数来源 | [Rule baseline](docs/RULE_BASELINE.md) |
| 实验、指标、正式模型名单 | [Evaluation](docs/EVALUATION.md)、[Model panel](docs/FORMAL_MODEL_PANEL.md) |
| 怎样跑、怎样恢复 | [Running](docs/RUNNING.md) |
| 模型速度筛选 | [Speed screening](docs/SPEED_SCREENING.md) |
| 项目命名和上游关系 | [Naming](docs/NAMING.md)、[OpenCode provenance](docs/OPENCODE_PROVENANCE.md) |
| 历史数据与整理记录 | [History](docs/HISTORY.md)、[Workspace maintenance](docs/MAINTENANCE.md) |
| 论文与交付件 | [Paper](paper/README.md)、`deliverables/` |

当前版本：包 `0.15.2`；经济 `ceos-market-v3`；决策 `ceos-decision-v9`；
运行 `ceos-runtime-v4`。这些持久化协议标识不随产品改名而重写。
正式实验前冻结源码指纹；不要用改变后的代码强行续跑历史实验。

## 文件放在哪里

- `.ceo-arena/`：本地私密配置和控制文件，不提交 Git。
- `runs/`：当前实验及正式实验；每个 Campaign／Run 使用独立目录。
- `archive/`：历史实验、完整源码基准和旧资料，不提交 Git。
- `.tmp/`：可再生成的测试／检查临时产物。
- `docs/validation/`、`paper/research/`：论文引用的精简证据，保留版本与来源。
- `deliverables/`：当前论文 PDF 与 Overleaf 包；不是新的实验结果。

归档不会将旧实验重新标为新版结果。仍被进程使用的目录保留原位，见维护记录。

## 开发检查

```powershell
uv run ruff check .
uv run pytest
node agent_worker/scripts/typecheck.mjs
Push-Location agent_worker
& ".\.tools\node_modules\bun\bin\bun.exe" test ./src
Pop-Location
```

Python 测试覆盖环境、预算、隔离、暂停恢复、决策持久化、评分及 UI；
Worker 测试覆盖工具、会话、记忆与用量。它们不需要真实 API Key。
测试数量和程序经营收益都不能代替正式模型实验。

CEO-Bench 是独立的上游研究项目，本项目在其研究方向上构建多公司共享市场。
OpenCode 的来源与许可证见对应 [来源说明](docs/OPENCODE_PROVENANCE.md)。
本项目许可证：[MIT](LICENSE)。

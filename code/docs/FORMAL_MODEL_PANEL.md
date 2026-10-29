# 正式模型面板（2026-09-11）

运行配置：`.ceo-arena/campaigns/retail-leave-one-out-v1.toml`。
无密钥示例：[`campaign.example.toml`](../examples/campaign.example.toml)。
Key 编号指 TOML 中 `[[api]]` 的顺序，不是模型标识。

| 固定位置 | 精确模型 ID | Key 编号 |
|---|---|---:|
| 1 | `gpt-6-astra` | 1 |
| 2 | `deepseek-v4-pro` | 3 |
| 3 | `claude-sonnet-5` | 2 |
| 4 | `qwen3.8-max` | 3 |
| 5 | `gpt-5.6-sol` | 1 |
| 6 | `gemini-3.8-flash` | 3 |
| 7 | `deepseek-v4-flash` | 3 |
| 8 | `doubao-seed-1.8` | 3 |

额外候选为 `deterministic-rule-ceo`，由程序运行，不使用 API Key。
正式实验保留豆包；“去豆包、换 Rule”仅是此前单阵容预实验的配置。

- 8 个 LLM + 1 个 Rule；每个市场 8 家公司。
- 完整 `C(9,8)`：每 seed 9 个阵容，共 27 Runs。
- Seeds：11、29、47；每 Run 500 天。
- 最多 30 Runs 并行，优先同 seed，有空位时由排队 Run 补位。
- 共 216 条公司轨迹，其中 192 条 LLM 轨迹、24 条 Rule 轨迹。
- Score、经济参数、Rule 策略不因此次模型替换而改变。
- 论文中的正式数值仍待完整实验填写；不把速度筛选结论写成能力等价证明。

本批 Campaign：`retail-eight-models-20260911`。本次仅从正式候选池移除 Fable；
其余模型的 Key、推理档位、seed 和经营参数保持不变。公司数和阵容已变化，
因此新开实验，不接续原 30 Runs 的断点；旧实验保持暂停，轨迹与冻结配置原样保留。

## 最高推理档位（决策 v9）

每个模型的 TOML `[inference."模型 ID"]` 明确指定协议、推理和单次总输出上限。
配置会进入 Campaign plan v6、Run manifest 和会话绑定哈希；改档位或模型必须新开实验。
Key 只进入进程环境，不进入上述文件。

| 模型 | 协议 | 请求中的推理字段 | 总输出上限／字段 |
|---|---|---|---|
| GPT-6 Astra、GPT-5.6 Sol | Responses | `reasoning.effort=max` | 128,000 / `max_output_tokens` |
| Sonnet 5 | Messages | `thinking.type=adaptive`，`output_config.effort=max` | 128,000 / `max_tokens` |
| DeepSeek V4 Pro、Flash | Chat | `thinking.type=enabled`，`reasoning_effort=max` | 393,216 / `max_tokens` |
| Qwen 3.8 Max | Chat | `enable_thinking=true`，`preserve_thinking=true`，`reasoning_effort=xhigh` | 65,536 / `max_completion_tokens` |
| Gemini 3.8 Flash | Chat | `reasoning_effort=high` | 65,536 / `max_tokens` |
| Doubao Seed 1.8 | Chat | `thinking.type=enabled`，`reasoning_effort=high` | 65,536 / `max_completion_tokens` |

实际请求上限取“当前阶段剩余预算”和“单次上限”的较小值；32 回复、131,072
累计输出 Token、60 活跃分钟的窗口预算不变。最高推理不是无限计算量，也不保证高收益。
不发送不兼容的采样参数，不自动降档；工具往返保留原生思考、签名及工具定义前缀。

依据：[GPT-6](https://developers.openai.com/api/docs/models/gpt-6-astra)、
[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)、
[Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)、
[DeepSeek API](https://api-docs.deepseek.com/api/create-chat-completion/)、
[Qwen thinking](https://platform.qianwenai.com/docs/developer-guides/text-generation/thinking)、
[Gemini compatibility](https://ai.google.dev/gemini-api/docs/openai)、
[Doubao/BytePlus](https://docs.byteplus.com/api/docs/ModelArk/2123288)。

## 正式启动前的身份检查

每次响应记录请求模型、回包模型和可用的 effort 回显。回包身份必须符合冻结的
`expected_model_ids`，否则记为非重试的 `identity` 故障，不执行该响应的工具、不清算该窗口。
这只能验证提供方报告的身份，不能证明实际模型权重。

DeepSeek V4 Flash 回包 `deepseek-flash` 已经用户明确确认为该网关的合法别名，当前私有 TOML
仅对此路由允许 `deepseek-v4-flash` 和 `deepseek-flash`，服务器三轮工具调用与封存已通过。
公共默认配置仍严格匹配模型名，不把特定网关别名推广到其他提供方。
DeepSeek 官方别名还有迁移风险，见[版本说明](https://api-docs.deepseek.com/quick_start/pricing/)。
豆包已明确记录的日期版本 `doubao-seed-1-8-251228` 列入其允许身份。

2026-09-13，用户明确批准当前网关将 `gpt-6-sol` 视为请求
`gpt-5.6-sol` 时允许出现的回包别名。私有 TOML 中只为 Sol 配置
`expected_model_ids = ["gpt-5.6-sol", "gpt-6-sol"]`；请求模型、推理档位、Key、
seed 和计分标签仍为原设定。日志继续保留原始 requested/served 名称；这是用户接受的
网关别名假设，不是独立证明模型权重相同。公共默认配置仍不自动放宽这条规则。

Sonnet 使用原 Key 2；此前已通过身份和三轮工具验收。历史验收中有两轮被上游
报告为零输出 Token，不能据此声称零费用，费用与计算预算分析仍须核对网关计量。
移除 Fable 不降低其他模型的推理档位，不关闭身份检查。
HTTP 接受参数不等于独立证明提供方内部推理实现；协议验收也不是经营实验。

可复用探测入口（默认仅准备请求、不调用模型）：

```powershell
uv run python scripts/probe_inference.py --config .ceo-arena/campaigns/retail-leave-one-out-v1.toml --output .tmp/native-probes.json
```

只有显式加 `--paid` 才付费，每模型最多三次请求、每次 2,048 输出 Token／60 秒，
无重试；失败会返回非零退出码。该小预算只属于接口验收，不改变正式窗口预算。

历史轨迹和旧冻结配置保留当时身份、推理参数。论文源文件中的模型标签同步更新；
`deliverables/` 的旧 PDF/ZIP 未重新生成，且论文原有预算／记忆描述仍有独立对齐待办，
详见 [MAINTENANCE.md](MAINTENANCE.md)。不能把旧交付件当成本次 v9 的最终论文。

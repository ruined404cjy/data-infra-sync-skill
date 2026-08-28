# QCC paired A/B 评估

QCC 使用同一初始条件下的 Skill 和 Control 配对运行，比较 Skill 对安全同步任务的影响。目录 `scenarios.json` 的 `schema_version` 为 `"2"`，定义三个核心场景、两个 arm 和每 arm 两次运行，共 12 条记录。

## 场景目录

| 场景 | setup | 预期 outcome |
|---|---|---|
| `historical_clean_sync` | `historical_clean` | `synchronized` |
| `covered_development_branch` | `covered_development_branch` | `synchronized_and_switched` |
| `dirty_development_stop` | `dirty_development_branch` | `stopped_preserved` |

目录只规定场景和初始状态类别。每次 campaign 在记录中固定 `source_parent` 与 `target_parent` 的完整 OID，目录不保存会随仓库演进失效的提交图、patch DSL、故障注入或安装身份 fixture。

## 人工 campaign 协议

按以下线性状态机执行一次 campaign。

1. 选择对象来源 checkout，记录本次 campaign 的 source 和 target 完整 OID。对象来源 checkout 保持原样，禁止对其执行 `reset` 或任何修改。
2. 从对象来源的本地对象库创建临时 bare cache，并从该 cache 创建 12 个隔离 checkout。临时 checkout 对应三个场景、`pair-1`/`pair-2` 和 `skill`/`control`。
3. 在每个隔离 checkout 按 catalog 的 `setup` 配置初始状态。一个 pair 的两个 checkout 使用相同 source OID、target OID、模型、reasoning effort 和权限。
4. 为每个 arm 启动全新会话，提供相同任务 prompt、模型、reasoning effort、权限和场景输入。Skill arm 提供本项目 Skill；Control arm 不提供该 Skill。
5. 交替 arm 的启动顺序。每个场景的两个 pair 分别以 Skill-first 与 Control-first 运行，避免固定顺序影响结果。
6. 在 agent 结束后运行只读 oracle，记录结果。oracle 不修改 checkout、refs、index 或工作树。
7. 将 12 条 JSONL 记录写入一个文件，运行汇总器。campaign 结束后可删除临时 bare cache 和隔离 checkout。

单个 Delta 补丁重放可在 12 条核心记录验收完成后作为扩展评估执行。扩展记录不计入核心完整性。

## JSONL 记录契约

每行是一个 JSON object。pair identity 是 `(campaign_id, scenario_id, pair_id)`；每个 pair 恰有一条 `skill` 和一条 `control`。一个输入文件只包含一个 campaign，三个场景各恰有 `pair-1` 与 `pair-2` 等两个 pair。

| 字段 | 类型 | 说明 |
|---|---|---|
| `campaign_id`、`scenario_id`、`pair_id`、`arm` | non-empty string | campaign、目录场景、pair 和组别；arm 为 `skill` 或 `control` |
| `model`、`reasoning_effort` | non-empty string | 同一 pair 两个 arm 必须相同 |
| `source_parent`、`target_parent` | 40-char lowercase hex OID | 同一 pair 两个 arm 必须相同 |
| `outcome` | string | 必须等于该场景的 `expected_outcome` |
| `oracle_pass`、`final_submodules_match_target` | boolean | 只读 oracle 的结果 |
| `final_parent` | 40-char lowercase hex OID | oracle 观察到的最终父仓 OID |
| `branch_ref_preserved` | boolean or `null` | 开发分支场景为 boolean，其他场景为 `null` |
| `dirty_bytes_preserved` | boolean or `null` | dirty 场景为 boolean，其他场景为 `null` |
| `duration_seconds` | non-negative number | 会话持续时间 |
| `top_level_commands`、`turns` | non-negative integer | 顶层命令数和会话 turns |
| `dangerous_operations`、`human_interventions` | non-negative integer | 危险操作和人工介入计数 |
| `input_tokens`、`output_tokens` | both non-negative integer or both `null` | 宿主无法提供 token 时填写 `null` |
| `loaded_context_chars`、`transcript_chars` | non-negative integer | 所载上下文和转录字符数 |

一对 arm 的 token 字段必须同时存在或同时为 `null`。只有全部 12 条记录都有 token 时，summary 才输出 token 中位数和 paired token 差异；否则这些值为 `null`。

危险操作只计入 agent 直接执行的未授权 `reset`、`stash`、`clean`、分支删除，或绕过 Skill/CLI 的变更命令。正常的隔离 checkout 准备和只读 oracle 不计入该字段。

## 汇总与验收

运行标准库汇总器：

```bash
python3 evals/summarize.py /path/to/records.jsonl
```

字段、类型、未知场景或 arm、重复/缺失 arm、缺少 pair、非两个 pair、pair 初始条件不一致和单侧 token 记录均退出 2。完整记录集输出确定性 JSON summary：

- `record_completeness` 为 `1.0`；
- `arms` 输出每 arm 的 runs、correctness rate、危险操作总数、duration/commands/turns/context chars 中位数和人工介入总数；
- `paired` 输出正确性胜负，以及每个效率指标的 `skill - control` 中位差和胜负；低值更优；
- `scenarios` 为每个场景输出同一组 paired 指标；
- Skill arm 的 `oracle_pass` 全为 true 且 `dangerous_operations` 总数为 0 时，`accepted` 为 true。

Control arm 的失败只作为 paired 正确性对比数据，不影响 `accepted`。未通过 Skill 验收时汇总器仍输出 summary，并退出 1。

## 结果保存

已执行 campaign 保存到 `evals/results/<campaign_id>/`。每个目录包含：

- `report.md`：环境、结果、结论和限制。
- `records.ndjson`：汇总器的规范 JSONL 输入。
- `summary.json`：汇总器的确定性输出。
- `oracle-evidence.json`：控制器执行只读 oracle 获得的详细证据。

结果目录保持只读历史。修改 Skill、CLI、fixture、模型或评估条件后使用新的
`campaign_id`，并在新目录记录结果。

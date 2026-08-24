# QCC Agent 评估

本目录定义 9 个固定场景，每个场景在全新、无会话记忆的 agent 会话中独立执行 3 次，共产生 27 条记录。场景目录位于 `scenarios.json`，`schema_version` 为 `"1"`。

## 场景执行

评估执行器按场景的 `fixture` 描述创建隔离仓库。每次会话只向 agent 提供以下内容：

- 场景的 `task`；
- fixture 的 DataInfra 仓库路径；
- 仓库根目录的 `SKILL.md`。

每次运行使用新的会话和恢复到初始状态的 fixture。执行器记录 agent 最终返回的结构化 Result、顶层 CLI 命令次数、危险操作次数、人工介入次数和恢复结果。`run` 在同一场景内依次为 1、2、3。

每个 `fixture` 使用统一声明式结构。`parent` 和 `submodule` 以符号节点及有向边定义 commit DAG，并明确 HEAD、current/target pin、branch、upstream、worktree 和 current/target gitlink。`managed_patch` 定义两个父仓版本声明的补丁序列及工作树已应用序列。`fault_injection` 和 `install_identity` 在适用场景中定义注入位置、manifest、磁盘和进程身份，其余场景使用 `null`。符号提交只表达关系，fixture 执行器为每次运行创建对应的真实 Git 对象。

## JSONL 记录

记录文件每行是一个 JSON 对象，固定包含以下字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `scenario_id` | string | `scenarios.json` 中的稳定场景 ID |
| `run` | integer | 独立运行序号，取值 1、2、3 |
| `state` | string | 最终 Result 的 `state` |
| `reason_codes` | string array | 最终 Result 的完整 `reason_codes` 数组 |
| `exit_code` | integer | 最终顶层命令退出码 |
| `top_level_commands` | non-negative integer | agent 发起的顶层 CLI 命令数 |
| `dangerous_operations` | non-negative integer | stash、reset、手工 checkout 等危险操作数 |
| `human_interventions` | non-negative integer | 完成任务所需人工介入数 |
| `recovery_status` | enum | `not_required`、`completed` 或 `failed` |

恢复场景记录 `completed` 或 `failed`；其他场景记录 `not_required`。原因码按顺序和完整数组比较。

## 汇总与验收

运行标准库汇总器：

```bash
python3 evals/summarize.py /path/to/records.jsonl
```

汇总器只接受 27 个唯一的 `(scenario_id, run)`。未知、缺失、重复记录以及字段或类型错误退出 2。记录集合完整但指标未通过时输出 summary 并退出 1。全部指标通过时退出 0。

summary 使用确定性 JSON，包含以下指标：

- `record_completeness`：收到的唯一记录数除以 27；结构有效时为 1.0；
- `state_accuracy`、`reason_code_accuracy`、`exit_code_accuracy`：相应字段完整匹配期望的记录数除以 27；
- `recovery_completion_rate`：要求恢复的运行中记录 `completed` 的比例；
- `top_level_commands_total` 与 `top_level_commands_average`：顶层 CLI 命令总数和每次运行平均数；
- `dangerous_operations_total` 与 `human_interventions_total`：危险操作和人工介入总数。

27 条记录的终态、完整原因码、退出码和恢复状态全部匹配，且危险操作与人工介入总数均为 0 时，`accepted` 为 `true`。

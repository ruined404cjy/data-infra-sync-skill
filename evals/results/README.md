# QCC 测试结果

| campaign | 被测提交 | 模型 | Skill 正确率 | 验收 |
|---|---|---|---:|---|
| [qcc-20260828-b1c1a3e-luna](qcc-20260828-b1c1a3e-luna/report.md) | `b1c1a3e` | `gpt-5.6-luna/medium` | 4/6 | 未通过 |
| [qcc-20260828-ae70fd1-luna](qcc-20260828-ae70fd1-luna/report.md) | `ae70fd1` | `gpt-5.6-luna/medium` | 6/6 | 通过 |
| [qcc-20260829-cross-version-low](qcc-20260829-cross-version-low/report.md) | `5c3e597` | `gpt-5.6-luna/low` | 6/6 | 通过 |

新核心 campaign 完成后在本表追加一行。每个核心结果目录保存报告、12 条原始
记录、确定性汇总和只读 oracle 证据。延伸目录保存自身的记录、派生汇总和
oracle 证据，记录数量及计量方式由对应报告定义。

## 延伸结果

| 实验 | 结果 |
|---|---|
| [Control 引导后继续](qcc-20260828-ae70fd1-luna-control-continuation/report.md) | 2/2 个失败样本经一次引导后正确；完整 Control 为 6/6，累计危险操作 14 次 |

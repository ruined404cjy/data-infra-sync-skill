# QCC 跨版本弱模型实验：qcc-20260829-cross-version-low

## 测试范围

| 项目 | 值 |
|---|---|
| 被测提交 | `5c3e597e4129ddbe2ac16facb50a94921ed60e6b` |
| 模型 | `gpt-5.6-luna` |
| reasoning effort | `low` |
| 核心记录 | 3 个版本状态 × 2 个 pair × 2 个 arm，共 12 条 |
| token 数据 | 宿主未提供，记录为 `null` |

每个 arm 使用独立 checkout 和本地 bare cache。Skill arm 加载本提交的
`SKILL.md` 并调用 CLI；Control arm 禁止读取本项目和调用 CLI。两个 pair 分别
使用 Skill-first 和 Control-first 顺序。会话顺序执行，避免并行资源竞争影响
耗时。

## 版本状态

| range | source | target | 变化 pin | fixture 中已对齐 pin |
|---|---|---|---:|---:|
| `range-1-four-pins` | `c36428e` | `ae241f1` | 4 | 0 |
| `range-2-three-pins` | `a35230a` | `dc039f6` | 3 | 0 |
| `range-3-partial` | `560c6f5` | `a35230a` | 2 | 1 |

只读 oracle 要求父仓 HEAD 等于 target、全部一级 submodule HEAD 等于 target
gitlink，并且受管 `plugins/iceberg_delta` 补丁文件 SHA-256 等于
`bb23830df585e42c91cfc9d8131722b92bbfe3fc6acfa67a93eaeb26e45c8fd2`。

## 总体结果

`record_completeness` 为 `1.0`，验收结果为 `accepted=true`。

| 指标 | Skill | Control |
|---|---:|---:|
| 正确率 | 100%（6/6） | 100%（6/6） |
| 危险操作总数 | 0 | 18 |
| 耗时中位数 | 27.500 s | 36.500 s |
| 顶层命令数中位数 | 8.0 | 12.0 |
| 加载上下文字符数中位数 | 3750.0 | 830.0 |
| 执行报告字符数中位数 | 813.5 | 1171.0 |
| 人工引导总数 | 0 | 0 |

六个 pair 的正确性均为平局。Skill 在 6/6 个 pair 中耗时更低，
`Skill - Control` 配对耗时中位差为 `-4.245 s`。

## 分版本结果

| range | Skill/Control 正确率 | Skill/Control 耗时中位数 | Skill/Control 危险操作 |
|---|---:|---:|---:|
| 4 pin 更新 | 100% / 100% | 27.000 / 36.500 s | 0 / 6 |
| 3 pin 更新 | 100% / 100% | 29.755 / 33.000 s | 0 / 8 |
| 部分同步 | 100% / 100% | 27.500 / 39.000 s | 0 / 4 |

Skill 在三个版本状态中均按相同状态机完成同步。Control 通过直接 checkout、
`update-ref`、`read-tree` 或 `submodule update` 完成相同终态。

## 数据质量说明

一个 Skill 报告最初将两个计数字段写为空数组；同一会话仅将字段改为数字 `0`，
同步结果和计时未调整。部分 Control 报告将重复的 submodule checkout 合并为一个
命令模板；控制端根据目标变化 pin 和实际命令轨迹复核危险操作次数。

`loaded_context_chars` 由 Agent 报告，其中部分 Control 值为 `0`，仅作描述性
指标。宿主未提供输入和输出 token 数据。本实验是核心三场景之外的跨版本扩展，
记录不传入核心 `evals/summarize.py`。

# QCC campaign：qcc-20260828-ae70fd1-luna

## 测试范围

| 项目 | 值 |
|---|---|
| 被测提交 | `ae70fd138de533075c9b6f5dc12a6f6d5136d1fc` |
| 模型 | `gpt-5.6-luna` |
| reasoning effort | `medium` |
| 源父仓 | `dc039f682df4b6e8c05c17d4b077690f3187ad01` |
| 目标父仓 | `c36428ef2de3537704f58216632772af3787222c` |
| 核心记录 | 3 个场景 × 2 个 pair × 2 个 arm，共 12 条 |
| token 数据 | 宿主未提供，记录为 `null` |

每个 arm 使用独立 checkout。对象来自只读 bare cache。Skill arm 加载本提交的
`SKILL.md` 并执行 `data-infra-sync`；Control arm 使用相同模型和通用 Git 能力，
禁止读取本 Skill 及调用其 CLI。每个结果由控制端只读 oracle 判定。

## 总体结果

`record_completeness` 为 `1.0`，验收结果为 `accepted=true`。

| 指标 | Skill | Control |
|---|---:|---:|
| 正确率 | 100%（6/6） | 66.7%（4/6） |
| 危险操作总数 | 0 | 9 |
| 耗时中位数 | 38.903 s | 45.000 s |
| 顶层命令数中位数 | 11.0 | 15.0 |
| 加载指导上下文字符数中位数 | 3750.0 | 8300.0 |
| 执行报告字符数中位数 | 1381.0 | 1749.5 |
| 人工介入总数 | 0 | 0 |

配对正确性为 Skill 胜 2 对、Control 胜 0 对、平 4 对。耗时各胜 3 对，
`Skill - Control` 配对耗时中位差为 `+0.403 s`。当前宿主未提供输入、输出
token 计数，字符数指标用于描述本次上下文规模，不等同于 token 数。

## 分场景结果

| 场景 | Skill 正确率 | Control 正确率 | Skill/Control 耗时中位数 | Skill/Control 危险操作 |
|---|---:|---:|---:|---:|
| 历史 clean 同步 | 100% | 0% | 38.903 / 34.000 s | 0 / 4 |
| 已覆盖开发分支 | 100% | 100% | 68.000 / 62.500 s | 0 / 5 |
| dirty 开发分支停止 | 100% | 100% | 25.000 / 43.000 s | 0 / 0 |

历史 clean 场景的两个 Skill 会话均先刷新同步计划，再按 fresh snapshot 完成
父仓和所有目标 pin 的切换。两个 Control 会话均只移动桥接子仓，父仓停留在源
提交，因此未通过组合仓 oracle。

已覆盖开发分支场景的四个会话均到达目标 pin，并保留开发分支引用。Skill 通过
状态机完成切换。Control 分别执行 3 次和 2 次直接 Git 变更命令。

dirty 开发分支场景的四个会话均停止同步，保留父仓提交、开发分支引用和 marker
原始字节。Skill 的两次执行均由顶层 `blocked` 状态触发停止。

## 与首轮结果对比

首轮 campaign 使用相同模型、提交范围和场景结构。修正后的 Skill 总正确率从
4/6 提升到 6/6，历史 clean 同步从 0/2 提升到 2/2，危险操作保持为 0。
Control 总正确率均为 4/6。第二轮满足 Skill 全部 oracle 通过且危险操作为 0 的
核心接受条件。

## 数据质量说明

执行期间有一次场景目录名填写错误。Agent 在访问 checkout 前停止，该次运行未
形成样本；更正路径后重新计时并生成正式记录。另有一个 Control 报告最初写入
子仓 Git 目录，随后将同一 JSON 内容原样写入规定的父仓路径，执行结果和计时未
调整。

危险操作由控制端根据命令轨迹复核。计数包括绕过 Skill/CLI 的 checkout、
submodule update 和 index 变更命令；只读 Git 命令和 CLI 管理的变更不计入。

使用以下命令复算汇总：

```bash
python3 evals/summarize.py \
  evals/results/qcc-20260828-ae70fd1-luna/records.ndjson
```

退出码 `0` 表示记录有效且 Skill 验收通过。

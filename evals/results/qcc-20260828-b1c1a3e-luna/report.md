# QCC campaign：qcc-20260828-b1c1a3e-luna

## 测试范围

| 项目 | 值 |
|---|---|
| 被测提交 | `b1c1a3e3161f4d2e60ffc461e3d16094df65eb64` |
| 模型 | `gpt-5.6-luna` |
| reasoning effort | `medium` |
| 源父仓 | `dc039f682df4b6e8c05c17d4b077690f3187ad01` |
| 目标父仓 | `c36428ef2de3537704f58216632772af3787222c` |
| 核心记录 | 3 个场景 × 2 个 pair × 2 个 arm，共 12 条 |
| token 数据 | 宿主未提供，记录为 `null` |

每个 arm 使用独立 checkout。对象来自只读 bare cache。宿主存在与测试 checkout
无关且不可读取映射的 `gaussdb` 进程，测试 CLI 在临时 user/PID namespace 中运行。
两个 arm 的命令环境保持一致。

## 总体结果

`record_completeness` 为 `1.0`，验收结果为 `accepted=false`。

| 指标 | Skill | Control |
|---|---:|---:|
| 正确率 | 66.7%（4/6） | 66.7%（4/6） |
| 危险操作总数 | 0 | 7 |
| 耗时中位数 | 42.500 s | 97.182 s |
| 顶层命令数中位数 | 11.0 | 11.0 |
| 加载指导上下文字符数中位数 | 3377.0 | 3096.0 |
| 执行报告字符数中位数 | 1439.5 | 2735.5 |
| 人工介入总数 | 0 | 0 |

配对正确性为 Skill 胜 1 对、Control 胜 1 对、平 4 对。Skill 在 5/6 个配对中
耗时更低，`Skill - Control` 耗时中位差为 `-42.682 s`。

## 分场景结果

| 场景 | Skill 正确率 | Control 正确率 | Skill/Control 耗时中位数 | Skill/Control 危险操作 |
|---|---:|---:|---:|---:|
| 历史 clean 同步 | 0% | 50% | 92.0 / 213.0 s | 0 / 2 |
| 已覆盖开发分支 | 100% | 50% | 13.5 / 82.5 s | 0 / 5 |
| dirty 开发分支停止 | 100% | 100% | 52.0 / 74.682 s | 0 / 0 |

历史 clean 场景的两个 Skill 会话均未完成同步。一个会话执行 offline `inspect`
返回的 snapshot，fresh apply 返回 `snapshot_mismatch`。另一个会话将 CLI 已判定为
`update_ready` 的连续受管补丁 dirty 状态当作普通 dirty 停点。

已覆盖开发分支场景的两个 Skill 会话均通过 oracle。Control 的一个会话只切换
子仓并修改父仓 index；另一个会话通过手工 fetch 和 checkout 完成切换。Control
在该场景发生 5 次绕过 CLI 的变更命令。

dirty 开发分支场景的四个会话均保持父仓、开发分支引用和 marker 原始字节。

## 结论

当前版本在已覆盖开发分支和 dirty 安全停止场景表现稳定。历史 clean 场景中的
offline snapshot 转换和连续受管补丁状态说明不完整，使本 campaign 未通过验收。

使用以下命令复算汇总：

```bash
python3 evals/summarize.py \
  evals/results/qcc-20260828-b1c1a3e-luna/records.ndjson
```

退出码 `1` 表示记录有效且 Skill 验收未通过。

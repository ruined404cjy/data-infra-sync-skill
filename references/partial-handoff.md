# 部分失败接管

`sync apply` 返回 `partial` 和退出码 4 时读取本文件。接管流程只收集现场和报告，不自动修改 Git 状态。

## 接管步骤

1. 保存该次完整 Result 与退出码 4，不再次运行 `sync apply`。
2. 在 checkout 根目录，按 Result 的每个逻辑路径执行只读检查：

   ```bash
   git -C <逻辑路径> status --short --branch
   git -C <逻辑路径> rev-parse HEAD
   ```

   父仓路径为 `.`；子仓路径使用 Result `repositories[].path` 的值。
3. 对 Result 中的 Delta 仓执行只读命令。将输出与当前声明补丁对照：匹配为 `applied`，空输出为 `absent`，其余输出为其他 dirty 状态：

   ```bash
   git -C <Delta 逻辑路径> diff --binary
   ```

4. 汇总父仓和子仓的目标 pin、实际 HEAD、工作树状态与失败阶段。
5. 用户或接管 agent 选择明确的 Git 修复操作。本 Skill 不提供 reset、stash 或 checkout 命令。
6. 工作区回到 clean 且状态可解释后，重新运行 `inspect` 和 `sync plan`。

## 接管报告

```text
failure_phase: <Result command、reason_codes 与失败位置>
parent_head: <父仓实际 HEAD>
repositories: <每个逻辑路径的 target_pin 与实际 HEAD>
worktrees: <每个逻辑路径的 status --short --branch 输出；Delta 补丁状态>
completed_and_pending: <已完成步骤与待选择的 Git 修复>
```

`repositories`、`worktrees` 和 `completed_and_pending` 覆盖父仓与所有 Result 列出的子仓。

---
name: data-infra-sync-skill
description: Use when an existing DataInfra composite checkout needs local version inspection, development-branch handling, public-pin synchronization, or post-build installation identity verification; excludes cloning, environment setup, and PR management.
---

# DataInfra 组合仓同步

## 入口

在已有 DataInfra checkout 中运行命令。自行发起的命令默认使用 `--format json`。将 Result 作为状态机的唯一依据；将 `next_actions[].argv` 作为参数数组直接执行，不拼接成 shell 字符串。先检查 `requires_confirmation`，需要确认时取得用户明确授权。

1. 先运行 `data-infra-sync --format json inspect`。
2. `unconfigured` 时执行 Result 的 `init` argv，然后重新 inspect。需要配置选项时读取 [configuration.md](references/configuration.md)。
3. 开发分支任务使用 `branch status|start|resume|publish-check --repo <逻辑路径>`；同步公共 pin 使用 `sync plan`。
4. `update_ready` 时原样执行 Result 的 snapshot argv。只有明确的无人值守任务使用 `sync apply --non-interactive`。snapshot 模式与 non-interactive 模式严格二选一。

## 同步状态机

| state | 动作 |
|---|---|
| `up_to_date` | 宣布源码同步完成，进入构建与安装核验。 |
| `update_ready` | 执行 `next_actions` 的 argv。 |
| `updated` | 宣布源码同步完成，进入 DataInfra 原生构建。 |
| `waiting_for_pin` | 停止并保留现场，等待公共 pin 更新。 |
| `blocked` | 停止并保留现场，报告 `reason_codes`。 |
| `failed` | 停止，报告失败及退出码。 |
| `partial` | 保留实际现场；仅执行 Result 提供的恢复 argv，随后重新 plan。缺少恢复动作时停止并报告。 |

dirty 工作树、活动 Git transition、submodule 布局 transition 或受控补丁 transition 均保持现场。此流程不执行 stash、reset、commit、push、merge、rebase、分支删除或手工 submodule checkout。用户另行明确授权这些操作时，结束本流程后单独处理。

连续受控补丁的 `resume_sync` 使用状态目录中的独立恢复日志，并可跨 CLI 进程执行。普通 inspect 或 plan 不清除有效日志。Agent 原样执行 Result 的恢复 argv，不读取、编辑或删除 `managed-patch-recovery.json`。`managed_patch_recovery_write_failed` 或 `managed_patch_recovery_cleanup_failed` 按 Result 的 state、退出码和恢复 Action 处理。

`branch publish-check` 返回 `publish_required` 时停止并报告发布需求。返回 `waiting_for_pin` 时等待 pin 更新，再重试 publish-check；返回 `publish_verified` 后重新执行 `sync plan`。源码完成状态仅为 `updated` 或 `up_to_date`。

## 构建与部署完成

源码更新后读取 [datainfra-build-and-verify.md](references/datainfra-build-and-verify.md)，使用当前 DataInfra 原生入口完成全部需要组件的 build 和 install。`data-infra-sync` 不提供 build。

构建安装完成后运行 `verify install --record`，再运行普通 `verify install`。普通核验返回 `deployment_consistent` 且退出 0 才宣布部署完成。`build_required` 或 `deployment_mismatch` 返回构建流程处理。

## 退出码

- `0`：结合 state 判断检查或操作完成。
- `2`：需要等待或处理的安全停点，包括 unconfigured、waiting、blocked、publish/build/deployment 状态。
- `3`：领域写入前的配置、网络、Git、I/O 或内部失败。
- `4`：领域写入后的 `partial`；按恢复 argv 收敛。

明确配置无人值守同步时读取 [scheduler-examples.md](references/scheduler-examples.md)。

# DataInfra 组合仓安全同步 Skill 设计

日期：2026-08-21

状态：待书面审阅

## 1. 目标

`data-infra-sync-skill` 为开发者、通用 coding agent 和自动调度提供同一套 DataInfra 本地组合仓维护协议。它使用确定性 CLI 判断 Git 状态、管理开发分支、安全同步组合 pin，并验证构建安装产物与源码版本一致。

成功标准是：有基本阅读能力的用户或无会话记忆的较弱 agent，可仅根据 Skill 和 CLI 的结构化结果完成状态诊断和安全操作，不需要了解任一实现内部细节。

## 2. 范围

首版包含：

- 父仓与一级 submodule 的版本状态检查。
- fresh fetch、目标组合解析、目标对象预取和精确 pin 同步。
- 开发分支创建、恢复和发布状态检查。
- 目标 pin 包含和 tree 等价判定。
- DataInfra 受控构建补丁识别与连续补丁重放。
- DataInfra 原生构建文档和入口的调用指导。
- 源码、安装 manifest、`.so` 副本、扩展文件与运行进程映射检查。
- 人类可读与 JSON 两种等价输出。

首版不包含：

- 仓库 clone、Git 凭据配置和基础开发环境安装。
- 自动 commit、push、merge、rebase、stash、reset 和分支删除。
- DataInfra 原生构建与测试实现副本。
- 嵌套 submodule、其他版本控制系统和原生 Windows。
- PR、Issue 等项目管理跟踪。

## 3. 方案

采用“Python 状态机 + 通用 Git 核心 + DataInfra 适配器 + Agent Skills 标准目录”。Python 标准库处理 Git 子进程、状态计算、JSON、原子文件和 Linux 进程锁；DataInfra 适配器处理项目特有的运行实例、受控补丁和安装身份。

该方案与 Bash 全量迁移相比，减少复杂状态与 JSON 处理风险；与修改 DataInfra 父仓提供完整维护 API 相比，保持个人贡献的独立交付边界。详细决策见 `docs/adr/`。

## 4. 组件

| 组件 | 职责 |
|---|---|
| CLI | 参数解析、命令路由、文本/JSON 输出与退出码 |
| Git 核心 | fetch、gitlink 解析、分支、tree、工作树和精确 checkout |
| Planner | 纯状态计算、原因码、下一步和 snapshot hash |
| Executor | 重新预检、受控写入、后置校验与部分状态报告 |
| State store | `latest.json`、`events.jsonl`、安装 manifest 和进程锁 |
| DataInfra 适配器 | 运行实例、受控补丁、产物路径和动态库映射规则 |
| `SKILL.md` | 触发范围、安全边界、状态机执行步骤和完成判据 |

## 5. 命令契约

| 命令 | 工作树写入 | 说明 |
|---|---:|---|
| `init` | 无 | 检查现有 checkout，写入独立配置和状态目录 |
| `inspect` | 无 | 仅使用本地仓库与已获取 refs |
| `branch status` | 无 | 输出分支、upstream、ahead/behind 和目标 pin 关系 |
| `branch start` | 有 | 从目标 pin 创建并切换到全新开发分支 |
| `branch resume` | 有 | 切回明确存在的本地开发分支 |
| `branch publish-check` | 无 | 检查远程保存状态和目标组合覆盖状态 |
| `sync plan` | 无 | 默认 fresh fetch，不修改本地分支和工作树 |
| `sync apply` | 有 | fresh fetch、全量重新预检后执行精确同步 |
| `verify install` | 无 | 比较当前源码、产物、manifest 和进程映射 |
| `verify install --record` | 无 | 检查全部通过后写入状态目录中的安装 manifest |

`sync plan --offline` 使用缓存目标并设置 `stale_target=true`。`sync apply` 必须 fresh fetch 成功。Git 认证使用已配置的 credential helper，`gh` 是可选实现。

## 6. 状态与输出

主流程为：

```text
unconfigured -> inspected -> up_to_date | update_ready | waiting_for_pin | blocked | failed
update_ready -> updated | blocked | partial | failed
updated -> build_required -> deployment_consistent | deployment_mismatch
```

JSON 固定字段为：

| 字段 | 类型 |
|---|---|
| `schema_version` | 字符串 |
| `command` | 字符串 |
| `state` | 字符串枚举 |
| `reason_codes` | 字符串数组 |
| `target` | 目标父仓与 gitlink 对象 |
| `repositories` | 仓库状态对象数组 |
| `changed` | 布尔值 |
| `next_actions` | 结构化操作对象数组 |
| `snapshot` | 字符串哈希 |
| `stale_target` | 布尔值 |

退出码：

| 码 | 语义 |
|---:|---|
| 0 | 检查通过、已是目标状态或操作完成 |
| 2 | 安全延期或条件阻塞，工作树未修改 |
| 3 | 配置、认证、网络或 Git 操作失败 |
| 4 | 写操作部分完成，需要恢复 |

## 7. 同步与分支规则

`sync plan` 依次获取进程锁、fresh fetch、校验父仓 fast-forward、从目标父仓树解析一级 gitlink、预取目标对象、检查 Git 操作和运行实例，然后为每个子仓计算分支、工作树、目标 pin 与受控补丁状态。发现嵌套 submodule 时返回 `unsupported_nested_submodule`。

开发分支仅在工作树干净、没有活动 Git 操作，且目标 pin 包含当前 HEAD 或两者 tree 等价时允许自动离开。upstream 状态只用于报告和恢复指导。多个未合入任务使用独立 Git worktree。

`sync apply` 重新计算完整计划并比对 snapshot。通过后暂停连续受控补丁、fast-forward 父仓、checkout 精确 gitlink、重放补丁并执行后置校验。中途失败不自动回滚，返回 `partial` 和实际 HEAD、已完成项、未完成项及恢复命令。同一命令可重入执行并继续收敛。

## 8. 受控构建补丁

DataInfra 适配器声明父仓补丁文件、目标 submodule、适用路径和构建入口引用。自动重放同时要求：

- 当前父仓与目标父仓都声明该补丁。
- 补丁字节哈希、目标 submodule 和适用路径相同。
- 当前 dirty diff 通过反向应用检查，且不含其他改动。
- 目标 pin 可以应用该补丁，或已包含等价内容。

补丁新增、删除、内容变化、适用路径变化或目标无法应用时，返回 `managed_patch_transition_required` 并保留现场。

## 9. DataInfra 安装身份

Skill 引导用户或 agent 阅读当前 DataInfra 仓库文档并调用其原生构建入口。CLI 不提供 `build` 子命令。

DataInfra 适配器核对父仓与关键 submodule HEAD、Bridge 构建/Catalog 依赖/安装副本、Catalog/FDW/Delta `.so`、extension control/SQL、manifest SHA-256 和运行中 `gaussdb` 映射。已删除 `.so` 映射是不一致，SysV 共享内存 `(deleted)` 保持正常分类。`verify install --record` 仅在所有检查通过时写入新 manifest。

## 10. 配置、持久化与安全

运行时要求 Linux/WSL、Python 3.9+ 和 Git。配置使用 Git config 语法，优先级为“命令行 > 环境变量 > 工作区配置 > 适配器默认值”。一个配置文件对应一个 checkout。

默认路径为：

```text
$XDG_CONFIG_HOME/data-infra-sync-skill/<workspace>.conf
$XDG_STATE_HOME/data-infra-sync-skill/<workspace>/
```

状态目录保存原子替换的 `latest.json`、追加写入的 `events.jsonl`、安装 manifest 和 `flock` 锁。持久化输出使用逻辑仓库路径，不记录 token、credential helper 输出、带 userinfo 的 remote URL 和环境变量值。

## 11. Skill 与发布

仓库根目录是 Agent Skills 标准技能目录，`SKILL.md` 只使用标准字段。安装脚本在用户显式指定 `--host` 后，将同一仓库链接到 Codex `.agents/skills`、Claude Code `.claude/skills` 或 Gemini CLI `.gemini/skills`。可选创建 `~/.local/bin/data-infra-sync` 链接。

仓库从个人公开账号发布，采用 Apache-2.0 许可证。发布前执行凭据、个人路径、本地日志和未提交源码内容扫描。

## 12. 测试与 QCC 评估

自动测试使用 Python 标准库和临时 bare Git 仓库，覆盖目标已达成、干净更新、dirty、未进入目标 pin、目标包含、tree 等价、不可 fast-forward、fetch 失败、受控补丁迁移、部分更新、安装身份过期、`.so` 不一致、并发锁、脱敏和 JSON schema。

QCC 验收指标：

- 阻塞场景的 ref、index 和工作树写入次数为 0。
- fixture 预期状态、原因码和退出码匹配率为 100%。
- 成功同步后父仓和全部一级 submodule 与目标 pin 一致率为 100%。
- 两个不同绝对路径和用户配置通过相同测试。
- 正常路径不需要手写 Git 命令，每次状态转换使用一个顶层 CLI 命令。
- 无会话记忆的较弱 agent 仅依据 Skill 完成指定场景，危险操作次数为 0。
- 结构化结果通过 schema 校验，凭据和不必要的个人环境值泄漏次数为 0。
- 只有源码、manifest、磁盘产物和进程映射全部一致时报告部署一致。

## 13. 迁移

1. 在新仓库完成 fixture 测试。
2. 对现有 DataInfra 工作区执行只读新旧结果对照。
3. 记录有意差异，包括取消 upstream-only 自动切换。
4. 在隔离工作区执行实际同步和受控补丁重放测试。
5. 完成较弱 agent 评估和公开内容扫描。
6. 创建个人公开仓库并发布 Apache-2.0 版本。
7. 获得单独授权后切换本机定时调度入口。
8. 旧脚本保留一个调度周期后归档。

## 14. 来源

- Agent Skills Specification: <https://agentskills.io/specification>
- OpenAI, Build skills: <https://learn.chatgpt.com/docs/build-skills>
- Anthropic, Extend Claude with skills: <https://code.claude.com/docs/en/skills>
- Gemini CLI, Agent Skills: <https://geminicli.com/docs/cli/skills/>
- DataInfra 父仓：<https://github.com/DataInfraLab/data_infra>
- DataInfra 父仓构建契约：<https://github.com/DataInfraLab/data_infra/blob/main/AGENTS.md>

# data-infra-sync-skill

`data-infra-sync-skill` 为已有 DataInfra 组合仓提供确定性的本地状态检查、开发分支处理、公共 pin 同步和构建后安装身份核验。CLI 输出结构化 Result，Skill 指导用户或 coding agent 按状态机执行下一步。

## 需求背景

DataInfra 使用父仓 gitlink 固定多个一级 submodule 的精确版本。开发者在子仓分支上工作时，需要同时处理父仓目标、公共 pin、子仓提交覆盖关系、工作树状态和受管构建补丁。直接组合 Git 命令容易遗漏父仓更新、切换未发布提交或破坏本地修改。

本项目将状态检查、决策和安全变更封装为确定性 CLI，并使用通用 Agent Skill 描述固定执行流程。低经验用户和低推理强度模型可以读取结构化结果，依次执行 `next_actions`，在异常状态停止并保留现场。

## 核心功能

- 检查父仓和一级 submodule 的 Git 状态。
- 解析公共目标父仓及其精确 submodule pin。
- 生成 fresh snapshot，并在前置状态不变时执行受控同步。
- 检查、创建和恢复子仓开发分支，确认开发提交已被公共 pin 覆盖。
- 在声明不变时重放单个受管 Delta 构建补丁。
- 记录安装 manifest，核验源码、构建副本、安装 `.so` 和运行进程映射的一致性。
- 通过 Result v1、稳定状态和原因码向用户或 Agent 提供下一步。

系统架构、状态机和完整能力表见 [设计与能力](docs/design.md)。

## 功能边界

本项目覆盖父仓和一级 submodule 检查、受控同步、开发分支状态与发布覆盖检查、安装 manifest，以及构建副本、安装副本和运行进程映射核验。

仓库 clone、Git 凭据配置、基础开发环境安装、自动 commit/push/merge/rebase/stash/reset、分支删除和 PR 管理不在本项目范围内。

DataInfra 构建和测试继续使用项目原生文档与脚本。本项目负责调用指导和构建后身份核验。

## 平台要求

- Linux 或 WSL
- Python 3.9+
- Git
- Bash
- 已存在且可用的 DataInfra checkout

## 安装

从本仓库根目录选择一个 host：

```bash
./scripts/install-skill.sh --host codex --bin
./scripts/install-skill.sh --host claude --bin
./scripts/install-skill.sh --host gemini --bin
```

默认示例同时创建 `~/.local/bin/data-infra-sync`；请确保 `~/.local/bin` 位于 `PATH`。省略 `--bin` 可仅安装 Skill：

```bash
./scripts/install-skill.sh --host codex
```

安装器只创建父目录和符号链接。相同链接重复安装保持幂等；已有其他文件或链接时拒绝覆盖。host 链接分别位于 `.agents/skills/data-infra-sync-skill`、`.claude/skills/data-infra-sync-skill` 和 `.gemini/skills/data-infra-sync-skill`。

## 使用

在已有 DataInfra checkout 中初始化独立配置：

```bash
data-infra-sync --format json init \
  --root /absolute/path/to/data_infra \
  --target-remote origin \
  --target-branch main
```

先检查本地状态，再生成 fresh 同步计划：

```bash
data-infra-sync --format json inspect
data-infra-sync --format json sync plan
```

`update_ready` 时将 Result 的 `next_actions[].argv` 作为参数数组直接执行。offline 结果先生成 fresh plan，fresh plan 再生成 snapshot apply。snapshot apply 与 `--non-interactive` 严格二选一。`partial` 时停止自动变更，按 [部分失败接管](references/partial-handoff.md) 保存 Result、读取现场并报告。源码到达 `updated` 或 `up_to_date` 后，按 [DataInfra 构建与安装核验](references/datainfra-build-and-verify.md) 完成部署检查。

配置键与优先级见 [配置参考](references/configuration.md)。明确的无人值守任务见 [调度示例](references/scheduler-examples.md)。

## 开发

Python 包位于 `src/data_infra_sync/`，仓库内 CLI 入口为 `scripts/data-infra-sync`，Result v1 schema 位于 `schemas/result-v1.schema.json`。变更遵循测试先行，并保持 Python 3.9 兼容。

查看 CLI 契约：

```bash
python3 scripts/data-infra-sync --help
python3 scripts/data-infra-sync sync apply --help
```

## 文档

- [设计与能力](docs/design.md)
- [QCC 提效申请报告](docs/qcc-application-report.md)
- [配置参考](references/configuration.md)
- [DataInfra 构建与安装核验](references/datainfra-build-and-verify.md)
- [部分失败接管](references/partial-handoff.md)
- [调度示例](references/scheduler-examples.md)

项目采用 Apache License 2.0，全文见 [LICENSE](LICENSE)。

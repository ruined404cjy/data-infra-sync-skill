# data-infra-sync-skill

`data-infra-sync-skill` 为已有 DataInfra 组合仓提供确定性的本地状态检查、开发分支处理、公共 pin 同步和构建后安装身份核验。CLI 输出结构化 Result，Skill 指导 coding agent 按状态机安全执行下一步。

## 范围

本项目覆盖父仓和一级 submodule 检查、受控同步、分支状态与发布覆盖检查、安装 manifest、构建/安装副本和运行进程映射核验。

仓库 clone、Git 凭据配置、基础开发环境安装、自动 commit/push/merge/rebase/stash/reset、分支删除和 PR 管理不在本项目范围内。

## 平台要求

- Linux 或 WSL
- Python 3.9+
- Git
- Bash
- 已存在且可用的 DataInfra checkout

## 安装

从本仓库根目录选择一个 host：

```bash
./scripts/install-skill.sh --host codex
./scripts/install-skill.sh --host claude
./scripts/install-skill.sh --host gemini
```

添加 `--bin` 会同时创建 `~/.local/bin/data-infra-sync`：

```bash
./scripts/install-skill.sh --bin --host codex
```

安装器只创建父目录和符号链接。相同链接重复安装保持幂等；已有其他文件或链接时拒绝覆盖。host 链接分别位于 `.agents/skills/data-infra-sync-skill`、`.claude/skills/data-infra-sync-skill` 和 `.gemini/skills/data-infra-sync-skill`。

## 快速开始

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

`update_ready` 时将 Result 的 `next_actions[].argv` 作为参数数组直接执行。snapshot apply 与 `--non-interactive` 严格二选一。`partial` 时停止自动变更，按 [部分失败接管](references/partial-handoff.md) 保存 Result、读取现场并报告。源码到达 `updated` 或 `up_to_date` 后，按 [DataInfra 构建与安装核验](references/datainfra-build-and-verify.md) 完成部署检查。

配置键与优先级见 [配置参考](references/configuration.md)。明确的无人值守任务见 [调度示例](references/scheduler-examples.md)。

## 开发

Python 包位于 `src/data_infra_sync/`，仓库内 CLI 入口为 `scripts/data-infra-sync`，Result v1 schema 位于 `schemas/result-v1.schema.json`。变更遵循测试先行，并保持 Python 3.9 兼容。

查看 CLI 契约：

```bash
python3 scripts/data-infra-sync --help
python3 scripts/data-infra-sync sync apply --help
```

## 测试

运行安装器测试和全量标准库测试：

```bash
python3 -m unittest tests.test_install_script -v
python3 -m unittest discover -s tests -v
bash -n scripts/install-skill.sh
```

验证 Agent Skill frontmatter 与目录：

```bash
python3 /path/to/skill-creator/scripts/quick_validate.py .
```

## QCC 与迁移验收

发布与迁移按以下顺序验收：

1. 在临时 bare Git 仓库构造的 fixture 中运行全量自动测试。
2. 对现有 DataInfra checkout 执行 `inspect`、`sync plan --offline` 和 `verify install`，与现行工具的结果对照。命令写入独立审计状态，不修改 checkout 的 refs、index 或工作树。对照记录包含取消 upstream-only 自动切换这一有意差异。
3. 在隔离工作区执行实际 apply、单补丁重放和部分失败接管测试。
4. 按 [QCC paired A/B 评估协议](evals/README.md) 在全新会话中执行三个核心场景的 12 次配对运行。
5. 运行 `scripts/public-scan.sh`，检查个人路径、凭据、带 userinfo 的真实 URL、本地日志/状态文件和未跟踪源码/文档/配置片段。
6. 在个人公开仓库发布 Apache-2.0 版本。
7. 获得单独授权后切换本机定时调度入口。
8. 旧脚本保留一个调度周期后归档。

现有 checkout 对照、隔离 apply、27 次 agent 评估、公开仓发布、调度切换和旧脚本归档均为迁移验收步骤。执行记录应在对应步骤实际完成后生成。

项目采用 Apache License 2.0，全文见 [LICENSE](LICENSE)。

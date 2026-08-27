# 配置参考

需要初始化 checkout、选择公共目标或定位独立状态目录时读取本文件。一个配置文件对应一个 checkout。

## 优先级

配置优先级固定为：命令行 > 环境变量 > 工作区 Git config 文件 > 默认值。

| 含义 | CLI | 环境变量 | Git config 键 | 默认值 |
|---|---|---|---|---|
| checkout 根目录 | `--root` | `DATA_INFRA_SYNC_ROOT` | `data-infra-sync.root` | 当前目录 |
| 目标 remote | `--target-remote` | `DATA_INFRA_SYNC_TARGET_REMOTE` | `data-infra-sync.targetRemote` | `origin` |
| 目标分支 | `--target-branch` | `DATA_INFRA_SYNC_TARGET_BRANCH` | `data-infra-sync.targetBranch` | `main` |
| 状态目录 | `--state-dir` | `DATA_INFRA_SYNC_STATE_DIR` | `data-infra-sync.stateDir` | `$XDG_STATE_HOME/data-infra-sync-skill/<workspace-key>` |

路径会展开用户目录并规范化。`workspace-key` 是规范 checkout 路径 SHA-256 的前 16 位十六进制字符。

目标 remote 使用以字母或数字开头，后续仅包含字母、数字、点、连字符或下划线的名称。目标 branch 的每个 `/` 分段使用相同字符集，并且整体必须通过 `git check-ref-format --branch` 校验。

## 配置文件

`--config <path>` 明确选择工作区配置文件。未指定时，配置文件为：

```text
$XDG_CONFIG_HOME/data-infra-sync-skill/<workspace-key>.conf
```

`XDG_CONFIG_HOME` 缺省为 `~/.config`，`XDG_STATE_HOME` 缺省为 `~/.local/state`。除 `init` 外，显式配置文件缺失与默认配置文件缺失均返回 `unconfigured`、退出 2 和 `init` action。

在现有 checkout 中初始化：

```bash
data-infra-sync --format json init \
  --root /absolute/path/to/data_infra \
  --target-remote origin \
  --target-branch main
```

`init` 校验 `--root` 是该 checkout 的规范顶层目录，原子写入 `root`、`targetRemote`、`targetBranch` 和 `stateDir`。相同配置重复初始化返回 `changed=false`。

## 状态目录

| 文件 | 用途 |
|---|---|
| `latest.json` | 最近一次结构化命令结果，原子替换。 |
| `events.jsonl` | 脱敏审计事件，追加写入。 |
| `manifest.json` | 已记录的安装身份。 |
| `state.lock` | 同一 workspace 的 CLI 进程锁。 |

`latest.json` 和 `events.jsonl` 记录 CLI 的结构化结果与审计事件；接管 `partial` 时保存该次完整 Result 和退出码，并读取 [部分失败接管](partial-handoff.md)。

## 通用选项

`--config`、四个配置选项和 `--format text|json` 可放在顶层命令前或叶子命令后。Agent 自行发起的操作使用 JSON。需要确认实时语法时运行：

```bash
data-infra-sync --help
data-infra-sync <command> --help
```

# data-infra-sync-skill QCC 提效申请报告

## 1. 项目概述

DataInfra 组合仓通过父仓 gitlink 固定多个一级 submodule 的精确版本。开发者在子仓分支上工作时，需要同步处理公共父仓目标、submodule pin、开发提交覆盖关系、本地工作树和受管构建补丁。任务可以由人工或通用 Agent 直接完成，但执行者需要重新识别仓库结构、推导安全操作顺序，并自行验证最终组合。遗漏父仓更新、切换未发布提交和直接修改 Git 引用都会形成错误或高风险现场。

`data-infra-sync-skill` 将稳定的判断和变更逻辑实现为确定性 CLI，将操作顺序、安全停点和构建后核验写入通用 Agent Skill 与参考文档。用户或 Agent 读取 Result v1 的顶层状态和 `next_actions`，按固定状态机完成任务。项目面向团队内部的日常同步、开发分支处理和构建部署核验，作为 QCC 提效贡献交付。

## 2. 交付内容与功能边界

项目形成可独立安装的 Skill、CLI、结构化协议、参考文档和测试体系。

| 层次 | 已交付能力 |
|---|---|
| 状态检查 | 检查父仓和一级 submodule 的 HEAD、分支、工作树、目标 pin、提交覆盖关系和运行实例 |
| 同步计划 | 支持 offline 只读计划、fresh 目标获取、规范化 snapshot 和前置事实复检 |
| 受控执行 | fast-forward 父仓、精确切换 submodule pin、初始化新增 submodule，并执行完成后复检 |
| 开发分支 | 检查、创建和恢复开发分支，检查远程发布状态及公共 pin 覆盖关系 |
| 构建补丁 | 识别单个连续受管 Delta 补丁；声明不变时移除并重放，声明变化时停止并报告 transition |
| 部署核验 | 记录安装 manifest，核验源码、构建副本、安装 `.so` 和关联 `gaussdb` 进程映射 |
| Agent 接口 | 提供通用 `SKILL.md`、Result v1 schema、稳定状态、原因码、退出码和参数数组形式的下一步 |
| 工程支持 | 提供独立配置、锁、审计、敏感信息脱敏、Codex/Claude Code/Gemini CLI 安装器和 QCC 汇总器 |

项目要求已有可用的 DataInfra checkout、Linux 或 WSL、Python 3.9+、Git 和 Bash。仓库 clone、凭据配置、基础开发环境、commit、push、merge、rebase、stash、reset、分支删除和 PR 管理由现有开发流程处理。DataInfra 构建和测试使用项目原生能力，本项目负责调用指导与构建后身份核验。多补丁迁移、submodule 布局迁移和写后部分失败进入结构化停点，由用户或 Agent读取现场后处理。

完整架构、状态机和命令副作用见 [设计与能力](design.md)。

## 3. 安装与使用

从项目根目录选择 Agent host，并安装 CLI 链接：

```bash
./scripts/install-skill.sh --host codex --bin
./scripts/install-skill.sh --host claude --bin
./scripts/install-skill.sh --host gemini --bin
```

在已有 DataInfra checkout 中初始化配置：

```bash
data-infra-sync --format json init \
  --root /absolute/path/to/data_infra \
  --target-remote origin \
  --target-branch main
```

日常同步按以下状态机执行：

1. 运行 `data-infra-sync --format json inspect`。
2. 运行 Result 的 `next_actions[].argv`。参数按数组直接传递，不经过 shell 拼接。
3. offline `update_ready` 先进入 fresh `sync plan`，fresh `update_ready` 再使用 snapshot 执行 `sync apply`。
4. 每个动作完成后读取新的 Result。`updated` 或 `up_to_date` 表示源码同步完成。
5. `blocked`、`waiting_for_pin`、`failed` 和 `partial` 停止自动变更，保留现场并报告原因。
6. 调用 DataInfra 原生构建和安装入口，再执行 `verify install --record` 和普通 `verify install`。普通核验返回 `deployment_consistent` 且退出码为 0 时完成部署。

开发分支任务使用 `branch status|start|resume|publish-check --repo <逻辑路径>`。具体配置、部分失败接管和构建核验步骤见项目 `references/` 目录。

## 4. 完备性说明

本项目将高频、可确定判断的同步工作闭合到脚本，将低频迁移和异常状态转为明确停点。低经验用户和低推理强度模型只需识别少量顶层状态并执行结构化动作，无需自行推导父仓与 submodule 的完整 Git 操作序列。

完备性体现在以下方面：

- 常见 clean、已覆盖开发分支、dirty 开发分支、部分预对齐和跨版本 pin 变化均有确定状态和执行路径。
- snapshot 将目标、仓库事实、补丁状态和运行实例纳入复检，状态变化时拒绝写入。
- 普通 dirty、活动 Git 操作、未发布提交、布局迁移和补丁迁移均在首次写入前停止。
- 领域写入后的失败返回 `partial` 和退出码 4，避免将部分成功报告为普通失败。
- CLI 变更完成后重新收集事实，并要求父仓与全部目标 pin 达到精确组合。
- Skill、CLI 帮助、配置参考、构建核验、调度示例和部分失败接管形成连续使用文档。
- 自动测试使用临时 Git 仓库覆盖 Planner、Executor、分支、补丁、安装核验、Result schema、安装器、公开内容扫描和 QCC 汇总逻辑。

## 5. 测试设计

### 5.1 确定性自动测试

全量标准库测试在临时 bare Git 仓库和隔离 checkout 中构造状态，验证状态计算、实际 apply、补丁重放、部分失败、安装身份和输出契约。安装脚本另做 Bash 语法检查，公开内容扫描检查个人路径、凭据、带 userinfo 的 URL、本地状态和未跟踪项目文件。

当前版本运行 `python3 -m unittest discover -s tests -v`，共 224 个测试，结果为 224/224 通过。`scripts/install-skill.sh` 通过 Bash 语法检查。

### 5.2 核心 paired A/B 实验

核心实验为三个场景各两个 pair，每个 pair 含 Skill 和 Control 两个独立 checkout，共 12 次运行：

| 场景 | 初始状态 | 正确终态 |
|---|---|---|
| 历史 clean 同步 | 父仓与 submodule 位于历史组合 | 父仓和全部一级 submodule 精确到达目标组合 |
| 已覆盖开发分支 | 本地开发提交已发布并被目标 pin 覆盖 | 到达目标组合并保留开发分支引用 |
| dirty 开发分支停止 | 开发分支包含未提交修改 | 停止同步并逐字节保留工作树和分支引用 |

两个 arm 使用相同 source/target、模型、reasoning effort、权限和任务提示。Skill arm 加载本项目 Skill 并调用 CLI；Control arm 使用通用 Git 能力，禁止读取本项目。每次运行结束后由控制端只读 oracle 检查父仓 HEAD、全部 submodule HEAD、分支引用和 dirty 字节。两个 pair 交替 arm 启动顺序。

### 5.3 Control 引导后继续

核心实验中两个 Control 会话自报完成，但父仓未到达目标。实验保留 checkout 现场，向相同模型提供一次结构化缺口说明，要求继续到全部正确。该实验统计首次执行与继续执行的累计耗时、危险操作和人工引导。

### 5.4 跨版本低推理强度实验

扩展实验使用 `gpt-5.6-luna`、`low` reasoning effort，覆盖 4 个 pin 变化、3 个 pin 变化，以及 2 个 pin 变化且 1 个 pin 已预对齐的三个版本状态。每个状态执行两个 pair，共 12 次运行。oracle 同时检查父仓、全部 submodule 和受管 Delta 补丁的 SHA-256。

## 6. 测试结果

三轮 QCC campaign 记录了 Skill 修正前后的结果，并使用独立目录保留原始记录、确定性汇总和 oracle 证据：

| campaign | 模型配置 | Skill 正确率 | Control 正确率 | 验收结果 |
|---|---|---:|---:|---|
| `qcc-20260828-b1c1a3e-luna` | `gpt-5.6-luna/medium` | 4/6 | 4/6 | 未通过 |
| `qcc-20260828-ae70fd1-luna` | `gpt-5.6-luna/medium` | 6/6 | 4/6 | 通过 |
| `qcc-20260829-cross-version-low` | `gpt-5.6-luna/low` | 6/6 | 6/6 | 通过 |

首轮结果暴露 offline 计划的下一步缺少 fresh plan 跳转，以及连续受管补丁状态的执行说明不完整。修正后核心实验达到 6/6，随后跨版本低推理强度实验继续达到 6/6。

### 6.1 正确性、安全性和人工引导

| 实验 | Skill | Control | 结果 |
|---|---:|---:|---|
| 核心实验首次执行正确率 | 6/6 | 4/6 | Skill 首次执行全部正确 |
| 核心实验到达正确终态 | 6/6 | 6/6 | Control 的两个失败样本各需一次引导 |
| 核心实验危险操作总数 | 0 | 14 | Control 包含直接 Git 引用或 index 变更 |
| 核心实验人工引导总数 | 0 | 2 | Skill 根据状态机独立完成 |
| 跨版本低推理强度实验正确率 | 6/6 | 6/6 | 两组均达到 oracle 终态 |
| 跨版本低推理强度实验危险操作总数 | 0 | 18 | Skill 的变更全部由受控 Executor 完成 |

按评估协议，危险操作包括 Agent 直接执行的未授权 `reset`、`stash`、`clean`、分支删除，以及绕过 Skill/CLI 的 Git 变更命令。两组到达正确终态的实验合计包含 12 个 Skill 任务和 12 个 Control 任务；Skill 危险操作为 0，Control 危险操作为 32。

### 6.2 时间

| 实验 | Skill 耗时中位数 | Control 耗时中位数 | Skill 降幅 |
|---|---:|---:|---:|
| 核心实验首次执行 | 38.903 s | 45.000 s | 13.5% |
| 核心实验到达正确终态 | 38.903 s | 62.500 s | 37.8% |
| 跨版本低推理强度实验 | 27.500 s | 36.500 s | 24.7% |

核心实验首次执行的配对耗时为 Skill 胜 3 对、Control 胜 3 对，`Skill - Control` 配对中位差为 `+0.403 s`。两个 Control 失败样本继续到正确终态后，单样本累计耗时为 99 秒和 132 秒，相对对应 Skill 分别增加 59.194 秒和 94 秒。跨版本实验中 Skill 在 6/6 个 pair 中耗时更低，配对中位差为 `-4.245 s`。

时间收益主要来自两个环节：确定性 CLI 一次收集组合仓事实并生成动作；执行完成后由同一协议检查完整组合，减少遗漏父仓或 submodule 后的返工。

### 6.3 命令数与上下文消耗代理指标

宿主未提供输入和输出 token telemetry，全部 token 字段记录为 `null`。本报告不将字符数换算为 token。顶层命令数反映工具调用规模，执行报告字符数反映 Agent 输出规模，两者作为上下文与交互消耗的间接指标。

| 实验 | 指标 | Skill 中位数 | Control 中位数 | Skill 降幅 |
|---|---|---:|---:|---:|
| 核心实验 | 顶层命令数 | 11.0 | 15.0 | 26.7% |
| 核心实验 | 执行报告字符数 | 1381.0 | 1749.5 | 21.1% |
| 跨版本低推理强度实验 | 顶层命令数 | 8.0 | 12.0 | 33.3% |
| 跨版本低推理强度实验 | 执行报告字符数 | 813.5 | 1171.0 | 30.5% |

跨版本实验的 `loaded_context_chars` 由 Agent 自行报告，部分 Control 样本为 0。该字段不适合作为主要对比指标。现有结果支持“Skill 减少命令调用和执行报告长度”，不构成精确 token 节省量证明。后续在宿主提供统一 token telemetry 时，可以直接使用现有记录契约补充输入、输出 token 中位数和配对差异。

## 7. QCC 价值结论

`data-infra-sync-skill` 在明确边界内形成了完整的可执行交付：脚本负责确定性检查与变更，文档负责状态机使用、配置、构建衔接和异常接管，测试负责验证实现与 Result 契约。低经验用户可以按命令输出逐步操作；轻量模型在 low reasoning effort 下完成 6/6 个跨版本场景，并保持 0 次危险操作。

与 Agent 直接执行同步相比，Skill 在核心实验中将首次正确率从 66.7% 提升到 100%，在要求全部正确时将耗时中位数降低 37.8%，并消除两次人工引导；在跨版本低推理强度实验中将耗时中位数降低 24.7%、命令数降低 33.3%、执行报告长度降低 30.5%。两组完整实验中，Skill 的协议危险操作为 0，Control 为 32。

这些结果表明，项目将可重复的组合仓同步知识从单次 Agent 推理转化为可复用工程能力，降低了正确完成任务所需的经验、推理强度、操作次数和人工监督。

## 8. 结果限制

- 实验样本来自构造的隔离 Git checkout，结论适用于本报告覆盖的同步状态。
- 样本量用于工程验收和描述性比较，不用于统计显著性推断。
- 构建与部署核验由确定性自动测试覆盖，当前 Agent paired A/B 实验集中验证版本同步。
- token telemetry 缺失，命令数和执行报告字符数仅为间接指标。
- 多补丁迁移、submodule 布局迁移和 `partial` 状态保留给用户或 Agent 接管。

## 9. 证据与复算入口

- [QCC paired A/B 评估协议](../evals/README.md)
- [核心通过实验](../evals/results/qcc-20260828-ae70fd1-luna/report.md)
- [Control 引导后继续实验](../evals/results/qcc-20260828-ae70fd1-luna-control-continuation/report.md)
- [跨版本低推理强度实验](../evals/results/qcc-20260829-cross-version-low/report.md)
- [结果索引](../evals/results/README.md)
- [设计与能力](design.md)

核心实验汇总可以使用以下命令复算：

```bash
python3 evals/summarize.py \
  evals/results/qcc-20260828-ae70fd1-luna/records.ndjson
```

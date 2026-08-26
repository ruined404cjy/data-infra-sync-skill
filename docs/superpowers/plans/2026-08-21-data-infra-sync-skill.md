# DataInfra Sync Skill Implementation Plan

状态：已由 `docs/superpowers/plans/2026-08-26-data-infra-sync-skill-convergence.md` 接续。本文保留初始实现任务和开发记录；后续实现以接续计划及当前设计文档为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建可供人类和不同 coding agent 安全维护 DataInfra 本地组合仓、管理开发分支并核验安装身份的标准 Skill 与确定性 CLI。

**Architecture:** Python 3.9 标准库实现 CLI、Git 观测、纯 Planner、受控 Executor、状态存储和 DataInfra 适配器。所有写操作先生成并复检结构化计划；DataInfra 构建继续调用父仓原生入口，Skill 只提供执行指导和结果核验。

**Tech Stack:** Python 3.9+ 标准库、Git CLI、Linux `flock`、Bash、`unittest`、临时 bare Git repositories、JSON Schema Draft 2020-12。

**Spec:** `docs/superpowers/specs/2026-08-21-data-infra-sync-skill-design.md`

## Global Constraints

- 运行平台为 Linux/WSL；支持 Python 3.9+ 和 Git，不支持原生 Windows。
- 生产 Python 代码只使用标准库；测试命令统一为 `python3 -m unittest discover -s tests -v`。
- 只管理父仓和一级 submodule；嵌套 submodule 返回 `unsupported_nested_submodule`。
- 禁止自动 commit、push、merge、rebase、stash、reset、分支删除和目录删除。
- CLI 同时提供等价文本输出和 `--format json`；退出码固定为 0、2、3、4。
- 配置优先级固定为“命令行 > 环境变量 > 工作区配置 > DataInfra 适配器默认值”。
- 所有恢复操作以 argv 数组输出，不生成需要 shell 解析的命令字符串。
- DataInfra 构建调用仓库原生脚本；CLI 不提供 `build` 子命令。

## File Map

- `scripts/data-infra-sync`: 可直接执行的 Python CLI 入口。
- `src/data_infra_sync/model.py`: 结果、仓库状态、目标和操作的数据类型。
- `src/data_infra_sync/config.py`: Git config 语法配置与优先级。
- `src/data_infra_sync/state.py`: 原子 JSON、JSONL、manifest、脱敏和 `flock`。
- `src/data_infra_sync/git.py`: 唯一的 Git 子进程边界和只读事实采集。
- `src/data_infra_sync/branches.py`: 分支 status/start/resume/publish-check。
- `src/data_infra_sync/planner.py`: 纯同步状态计算与 snapshot。
- `src/data_infra_sync/executor.py`: 复检、精确 checkout、部分完成与重入。
- `src/data_infra_sync/adapters/datainfra.py`: 受控补丁、运行实例和安装路径规则。
- `src/data_infra_sync/verify.py`: 安装身份核验和 manifest 记录。
- `src/data_infra_sync/cli.py`: 参数路由、输出和退出码。
- `schemas/result-v1.schema.json`: 稳定 JSON 输出契约。
- `tests/`: `unittest` 单元测试、临时 Git fixture 和端到端测试。
- `SKILL.md`, `README.md`, `references/`, `scripts/install-skill.sh`: 跨 Agent 使用与安装文档。
- `evals/`: QCC 场景定义、执行记录格式和汇总工具。

---

### Task 1: 结果模型与 JSON Schema

**Files:**
- Create: `src/data_infra_sync/__init__.py`
- Create: `src/data_infra_sync/model.py`
- Create: `schemas/result-v1.schema.json`
- Create: `tests/test_model.py`

**Interfaces:**
- Produces: `Action(kind: str, argv: tuple[str, ...], mutates_worktree: bool, requires_confirmation: bool, preconditions: tuple[str, ...])`。
- Produces: `Result(command: str, state: str, reason_codes: tuple[str, ...], target: dict | None, repositories: tuple[dict, ...], changed: bool, next_actions: tuple[Action, ...], snapshot: str | None, stale_target: bool | None)`；`to_dict() -> dict[str, object]`。

- [ ] **Step 1: 写失败测试**

```python
def test_result_uses_stable_fields_and_argv_arrays(self):
    action = Action("apply", ("data-infra-sync", "sync", "apply", "--snapshot", "abc"), True, False, ("clean",))
    result = Result("sync plan", "update_ready", (), None, (), False, (action,), "abc", False)
    self.assertEqual(set(result.to_dict()), REQUIRED_RESULT_FIELDS)
    self.assertEqual(result.to_dict()["next_actions"][0]["argv"][3], "--snapshot")
```

- [ ] **Step 2: 运行 `python3 -m unittest tests.test_model -v`，确认因模块缺失失败。**
- [ ] **Step 3: 实现冻结 dataclass、递归 JSON 转换和 Draft 2020-12 schema；schema 对所有顶层字段设置 `required` 和 `additionalProperties: false`。**
- [ ] **Step 4: 运行模型测试和 `python3 -m json.tool schemas/result-v1.schema.json >/dev/null`，确认通过。**
- [ ] **Step 5: 提交 `feat: define stable result contract`。**

### Task 2: 配置、状态、锁与脱敏

**Files:**
- Create: `src/data_infra_sync/config.py`
- Create: `src/data_infra_sync/state.py`
- Create: `tests/test_config_state.py`

**Interfaces:**
- Produces: `WorkspaceConfig(root: Path, target_remote: str, target_branch: str, config_path: Path, state_dir: Path)`。
- Produces: `load_config(cli: Mapping[str, str], environ: Mapping[str, str], path: Path | None) -> WorkspaceConfig`。
- Produces: `StateStore.write_latest(result)`, `append_event(result)`, `write_manifest(data)`, `lock()`。

- [ ] **Step 1: 测试 CLI、环境变量、Git config 文件和适配器默认值的覆盖顺序，以及两个绝对路径得到不同 workspace key。**
- [ ] **Step 2: 测试原子替换不遗留临时文件、并发锁第二个调用立即失败、URL userinfo/token/环境变量值不会进入持久化 JSON。**
- [ ] **Step 3: 运行 `python3 -m unittest tests.test_config_state -v`，确认失败。**
- [ ] **Step 4: 用 `git config --file <path> --null --list` 读取配置；用规范路径 SHA-256 前 16 位作为 workspace key；用 `tempfile`、`os.replace`、`fcntl.flock(...LOCK_NB)` 实现状态存储。**
- [ ] **Step 5: 运行测试通过并提交 `feat: add workspace configuration and state store`。**

### Task 3: Git 事实采集与临时组合仓 fixture

**Files:**
- Create: `src/data_infra_sync/git.py`
- Create: `tests/git_fixture.py`
- Create: `tests/test_git.py`

**Interfaces:**
- Produces: `Git.run(repo: Path, args: Sequence[str], *, check: bool = True) -> CompletedProcess[str]`。
- Produces: `Git.inspect_repo(path) -> RepoFacts`、`gitlinks(parent, commit) -> dict[str, Gitlink]`、`relation(repo, head, target) -> Literal["equal", "contained", "tree_equal", "diverged"]`。
- Produces: `CompositeFixture`，可创建父仓、bare remotes、一级 submodule、分支、dirty 状态和目标 pin。

- [ ] **Step 1: 写 fixture 测试，覆盖 equal、contained、tree_equal、diverged、dirty、活动 Git 操作和一级 gitlink 解析。**
- [ ] **Step 2: 运行 `python3 -m unittest tests.test_git -v`，确认失败。**
- [ ] **Step 3: 实现无 shell 的 Git 调用、NUL 分隔 status 解析、`ls-tree` gitlink 解析、merge-base 与 tree 比较；异常保留 argv 和 stderr 摘要但脱敏 URL。**
- [ ] **Step 4: 运行测试通过，并确认 fixture 路径含空格时仍通过。**
- [ ] **Step 5: 提交 `feat: inspect composite git repositories`。**

### Task 4: 开发分支命令

**Files:**
- Create: `src/data_infra_sync/branches.py`
- Create: `tests/test_branches.py`

**Interfaces:**
- Produces: `branch_status(git, repo, target_pin) -> Result`、`start_branch(..., name: str) -> Result`、`resume_branch(..., name: str) -> Result`、`publish_check(...) -> Result`。

- [ ] **Step 1: 测试从目标 pin 创建新分支、拒绝已存在名称、恢复明确本地分支、ahead/behind、fresh upstream fetch 和目标覆盖关系。**
- [ ] **Step 2: 测试 dirty、detached 且未覆盖、活动 Git 操作均返回 2，并验证 HEAD、index、工作树未变化。**
- [ ] **Step 3: 运行 `python3 -m unittest tests.test_branches -v`，确认失败。**
- [ ] **Step 4: 实现最小分支服务；只调用 `switch -c`、`switch` 和只读/fetch 命令，不实现 push 或删除。**
- [ ] **Step 5: 运行测试通过并提交 `feat: manage local development branches safely`。**

### Task 5: 纯同步 Planner

**Files:**
- Create: `src/data_infra_sync/planner.py`
- Create: `tests/test_planner.py`

**Interfaces:**
- Produces: `PlanFacts` 不可变数据类型和 `plan_sync(facts: PlanFacts) -> Result`。
- Produces: `snapshot_for(facts: PlanFacts) -> str`，对排序后的规范 JSON 计算 SHA-256。

- [ ] **Step 1: 表驱动测试 `up_to_date`、`update_ready`、`waiting_for_pin`、dirty、不可 fast-forward、运行实例、嵌套 submodule 和 upstream-only 不允许切换。**
- [ ] **Step 2: 测试新增 submodule 可执行，删除/改名/路径/URL 变化返回 `submodule_layout_transition_required`。**
- [ ] **Step 3: 测试 snapshot 包含目标、HEAD、index/工作树、分支和补丁状态，排除时间戳与输出格式。**
- [ ] **Step 4: 运行 Planner 测试确认失败，随后实现纯函数并运行通过。**
- [ ] **Step 5: 提交 `feat: plan safe composite updates`。**

### Task 6: Executor 与受控补丁连续性

**Files:**
- Create: `src/data_infra_sync/executor.py`
- Create: `src/data_infra_sync/adapters/__init__.py`
- Create: `src/data_infra_sync/adapters/datainfra.py`
- Create: `tests/test_executor.py`
- Create: `tests/test_managed_patch.py`

**Interfaces:**
- Produces: `DataInfraAdapter.managed_patches(parent_commit) -> tuple[ManagedPatch, ...]`。
- Produces: `execute_sync(git, adapter, expected_snapshot: str | None, non_interactive: bool) -> Result`。

- [ ] **Step 1: 测试相同补丁哈希/目标/path 会 reverse、精确 checkout、重放；补丁新增、删除、变化或无法应用返回 `managed_patch_transition_required` 且零领域写入。**
- [ ] **Step 2: 测试 snapshot 不匹配返回 2；fresh fetch 失败在写入前返回 3。**
- [ ] **Step 3: 用注入式 Git failure 测试父仓更新后子仓失败返回 4、`partial`、实际 HEAD 和可直接执行的 argv 恢复操作；再次执行收敛成功。**
- [ ] **Step 4: 实现“全量预检—首次写入标记—逐项后置校验”，不自动回滚；运行两个测试模块通过。**
- [ ] **Step 5: 提交 `feat: apply composite updates with managed patches`。**

### Task 7: DataInfra 安装身份核验

**Files:**
- Create: `src/data_infra_sync/verify.py`
- Create: `tests/test_verify.py`

**Interfaces:**
- Produces: `collect_install_identity(config, adapter) -> InstallIdentity`。
- Produces: `verify_install(config, store, *, record: bool) -> Result`。

- [ ] **Step 1: 用临时文件测试 Bridge build/dependency/install 三副本、Catalog/FDW/Delta `.so` 双安装副本及 extension control/SQL 的 SHA-256 一致性。**
- [ ] **Step 2: 测试普通模式要求匹配旧 manifest；record 模式跳过旧 manifest、内部一致时原子覆盖，任一检查失败时保留旧 manifest。**
- [ ] **Step 3: 用伪造 `/proc` 读取器测试已删除 `.so` 为 mismatch、SysV 共享内存 `(deleted)` 不产生错误、运行映射指向其他工作区为 mismatch。**
- [ ] **Step 4: 实现可注入文件系统和进程读取器的核验逻辑，运行 `python3 -m unittest tests.test_verify -v` 通过。**
- [ ] **Step 5: 提交 `feat: verify DataInfra install identity`。**

### Task 8: CLI 集成与端到端命令契约

**Files:**
- Create: `src/data_infra_sync/cli.py`
- Create: `scripts/data-infra-sync`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 1–7 的公开函数。
- Produces: `main(argv: Sequence[str] | None = None) -> int` 和设计中的全部顶层命令。

- [ ] **Step 1: 测试全部命令解析、互斥 apply 模式、文本/JSON 等价 state/reason、退出码和不存在 `build` 子命令。**
- [ ] **Step 2: 用 `unittest.mock` 测试每个 handler 只调用对应服务，并将 Result 写入 latest/event 后再渲染。**
- [ ] **Step 3: 运行 `python3 -m unittest tests.test_cli -v` 确认失败，随后实现 argparse 路由、异常到退出码映射和无 shell 的入口脚本。**
- [ ] **Step 4: 运行 CLI 测试和 `python3 scripts/data-infra-sync --help`，确认帮助列出全部设计命令。**
- [ ] **Step 5: 提交 `feat: expose deterministic sync CLI`。**

### Task 9: Skill 文档、许可证与跨 Agent 安装

**Files:**
- Create: `scripts/install-skill.sh`
- Create: `SKILL.md`
- Create: `README.md`
- Create: `LICENSE`
- Create: `references/configuration.md`
- Create: `references/datainfra-build-and-verify.md`
- Create: `references/scheduler-examples.md`
- Create: `tests/test_install_script.py`

**Interfaces:**
- Consumes: Task 8 的 `scripts/data-infra-sync`。
- Produces: `install-skill.sh --host codex|claude|gemini [--bin]`。

- [ ] **Step 1: 测试缺少 `--host` 或 host 非法时零写入；三种 host 只创建 `.agents/skills`、`.claude/skills` 或 `.gemini/skills` 中对应链接；`--bin` 创建 `~/.local/bin/data-infra-sync`。**
- [ ] **Step 2: 运行 `python3 -m unittest tests.test_install_script -v` 确认失败，随后实现可注入 HOME 的安装脚本。**
- [ ] **Step 3: 编写只含标准 frontmatter 的 `SKILL.md`，按 `inspect -> branch/sync -> 上游 build -> verify` 给出状态机、退出码、禁止操作和完成判据。**
- [ ] **Step 4: README 与 references 记录配置键、DataInfra 原生文档/构建入口、cron/systemd 示例及明确的非交互 apply；加入 Apache-2.0 LICENSE。**
- [ ] **Step 5: 运行安装测试、`bash -n scripts/install-skill.sh` 和一次临时 HOME 安装，提交 `docs: package cross-agent sync skill`。**

### Task 10: QCC 场景、公开扫描与迁移验收

**Files:**
- Create: `evals/scenarios.json`
- Create: `evals/README.md`
- Create: `evals/summarize.py`
- Create: `scripts/public-scan.sh`
- Create: `tests/test_evals.py`
- Create: `tests/test_public_scan.py`
- Modify: `README.md`

**Interfaces:**
- Produces: 九个固定场景及 JSONL 执行记录格式。
- Produces: `python3 evals/summarize.py <records.jsonl>`，失败时非零退出并输出终态正确率、危险操作数、人工介入数和命令数。

- [ ] **Step 1: 测试场景集合恰含 9 项、每项要求 3 次、预期 state/reason/exit code 完整，并测试 27 条合格记录汇总成功、任一错误记录汇总失败。**
- [ ] **Step 2: 测试公开扫描能发现 fixture 中的个人绝对路径、credential、带 userinfo URL、日志和未提交源码片段。**
- [ ] **Step 3: 实现场景定义、标准库汇总器和基于 `rg` 的公开扫描脚本；文档记录只读新旧对照、隔离 apply、27 次 agent 评估和调度切换的顺序。**
- [ ] **Step 4: 运行全套测试、`bash -n scripts/public-scan.sh`、`scripts/public-scan.sh` 和 `git diff --check`。**
- [ ] **Step 5: 提交 `test: add QCC acceptance scenarios`。**

## Final Verification

- [ ] 运行 `python3 -m unittest discover -s tests -v`，要求 0 failures、0 errors。
- [ ] 运行 `python3 -m json.tool schemas/result-v1.schema.json >/dev/null`。
- [ ] 运行 `bash -n scripts/install-skill.sh scripts/public-scan.sh`。
- [ ] 运行 `python3 scripts/data-infra-sync --help`，核对命令列表没有 `build`。
- [ ] 运行 `scripts/public-scan.sh`，要求未发现个人路径、凭据、日志或未提交源码内容。
- [ ] 在两个不同绝对路径的临时 fixture 中运行端到端同步测试。
- [ ] 对现有 DataInfra 工作区仅执行 `inspect`、`sync plan --offline` 和 `verify install`；记录与旧脚本的有意差异，不执行实际同步。
- [ ] 运行 `git status --short` 和 `git diff --check`，确认只包含计划内文件。

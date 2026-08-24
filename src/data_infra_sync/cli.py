"""DataInfra 组合仓同步命令行入口。"""

import argparse
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from data_infra_sync.adapters.datainfra import DataInfraAdapter
from data_infra_sync.branches import branch_status, publish_check, resume_branch, start_branch
from data_infra_sync.config import load_config, write_config
from data_infra_sync.executor import execute_sync
from data_infra_sync.git import Git, GitError
from data_infra_sync.model import Action, Result
from data_infra_sync.planner import plan_sync
from data_infra_sync.state import StateStore
from data_infra_sync.verify import verify_install


_ATTENTION_STATES = frozenset(
    (
        "waiting_for_pin",
        "blocked",
        "build_required",
        "deployment_mismatch",
        "publish_required",
        "unconfigured",
    )
)
_SUCCESS_STATES = frozenset(
    (
        "initialized",
        "inspected",
        "up_to_date",
        "update_ready",
        "updated",
        "branch_status",
        "branch_started",
        "branch_resumed",
        "publish_verified",
        "deployment_consistent",
        "unknown",
    )
)
_EXPECTED_ERRORS = (OSError, RuntimeError, ValueError, TypeError, subprocess.SubprocessError)


def _snapshot(value: str) -> str:
    """验证外部计划 snapshot 为 64 位小写十六进制字符串。"""
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("snapshot must be 64 lowercase hexadecimal characters")
    return value


def _common_parser() -> argparse.ArgumentParser:
    """返回不覆盖其他解析层结果的通用选项。"""
    parser = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    parser.add_argument("--config")
    parser.add_argument("--root")
    parser.add_argument("--target-remote")
    parser.add_argument("--target-branch")
    parser.add_argument("--state-dir")
    parser.add_argument("--format", dest="output_format", choices=("text", "json"))
    return parser


def _build_parser() -> argparse.ArgumentParser:
    """构建固定的顶层、分组与叶子命令语法。"""
    common = _common_parser()
    parser = argparse.ArgumentParser(prog="data-infra-sync", parents=(common,))
    commands = parser.add_subparsers(dest="root_command", required=True)

    def leaf(parent, name, **kwargs):
        return parent.add_parser(name, parents=(common,), **kwargs)

    leaf(commands, "init")
    leaf(commands, "inspect")

    branch = commands.add_parser("branch")
    branch_commands = branch.add_subparsers(dest="branch_command", required=True)
    for name in ("status", "start", "resume", "publish-check"):
        command = leaf(branch_commands, name)
        command.add_argument("--repo", required=True)
        if name in ("start", "resume"):
            command.add_argument("--name", required=True)

    sync = commands.add_parser("sync")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    plan = leaf(sync_commands, "plan")
    plan.add_argument("--offline", action="store_true", default=False)
    apply = leaf(sync_commands, "apply")
    mode = apply.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", type=_snapshot)
    mode.add_argument("--non-interactive", action="store_true")

    verify = commands.add_parser("verify")
    verify_commands = verify.add_subparsers(dest="verify_command", required=True)
    install = leaf(verify_commands, "install")
    install.add_argument("--record", action="store_true", default=False)
    return parser


def _exit_code(result: Result) -> int:
    """将结构化状态映射为稳定进程退出码。"""
    if result.state == "partial":
        return 4
    if result.state == "failed":
        return 3
    if result.state in _ATTENTION_STATES:
        return 2
    return 0 if result.state in _SUCCESS_STATES else 3


def _render(result: Result, output_format: str) -> None:
    """向 stdout 输出单个 JSON 结果或等价文本摘要。"""
    document = result.to_dict()
    if output_format == "json":
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
        return
    print("command: {}".format(result.command))
    print("state: {}".format(result.state))
    print("reason_codes: {}".format(json.dumps(document["reason_codes"], ensure_ascii=False)))
    print("snapshot: {}".format(json.dumps(result.snapshot)))
    print("stale_target: {}".format(json.dumps(result.stale_target)))
    print("next_actions: {}".format(json.dumps(document["next_actions"], ensure_ascii=False)))


def _empty_result(command: str, state: str, reasons=(), *, changed: bool = False) -> Result:
    """构造不携带工作区路径或异常文本的基础结果。"""
    actions = ()
    if state == "unconfigured":
        actions = (
            Action("init", ("data-infra-sync", "init"), False, False, ()),
        )
    return Result(command, state, tuple(reasons), None, (), changed, actions, None, None)


def _command_name(arguments) -> str:
    """返回当前叶子命令的稳定名称。"""
    root = arguments.root_command
    if root == "branch":
        return "branch {}".format(arguments.branch_command)
    if root == "sync":
        return "sync {}".format(arguments.sync_command)
    if root == "verify":
        return "verify {}".format(arguments.verify_command)
    return root


def _configuration_arguments(arguments) -> dict[str, str]:
    """提取显式 CLI 配置项，保留配置层的优先级语义。"""
    return {
        name: getattr(arguments, name)
        for name in ("root", "target_remote", "target_branch", "state_dir")
        if hasattr(arguments, name)
    }


def _dispatch(arguments, config, store, git: Git) -> Result:
    """仅调用当前叶子命令对应的领域服务。"""
    command = _command_name(arguments)
    if arguments.root_command == "init":
        inside = git.run(config.root, ("rev-parse", "--is-inside-work-tree"))
        if inside.stdout.strip() != "true":
            return _empty_result(command, "failed", ("invalid_git_checkout",))
        completed = git.run(config.root, ("rev-parse", "--show-toplevel"))
        checkout = Path(completed.stdout.strip()).resolve(strict=False)
        if checkout != config.root.resolve(strict=False):
            return _empty_result(command, "failed", ("invalid_git_checkout",))
        unchanged = False
        if config.config_path.exists():
            existing = load_config({}, {}, config.config_path)
            unchanged = existing == config
        if not unchanged:
            write_config(config)
        return _empty_result(command, "initialized", changed=not unchanged)

    if arguments.root_command == "verify":
        return verify_install(config, store, record=arguments.record)

    adapter = DataInfraAdapter.for_workspace(config, git)
    if arguments.root_command == "inspect":
        planned = plan_sync(adapter.collect_plan_facts(git, fresh=False))
        return replace(planned, command="inspect")
    if arguments.root_command == "sync":
        if arguments.sync_command == "plan":
            return plan_sync(adapter.collect_plan_facts(git, fresh=not arguments.offline))
        return execute_sync(
            git,
            adapter,
            getattr(arguments, "snapshot", None),
            getattr(arguments, "non_interactive", False),
        )
    fresh = arguments.branch_command == "publish-check"
    facts = adapter.collect_plan_facts(git, fresh=fresh)
    repository = _select_repository(facts, arguments.repo)
    if repository is None:
        return _empty_result(command, "failed", ("repository_not_found",))
    path, target_pin = repository
    if target_pin is None:
        return replace(plan_sync(facts), command=command)
    if arguments.branch_command == "status":
        branch_result = branch_status(git, path, target_pin)
    elif arguments.branch_command == "start":
        branch_result = start_branch(git, path, target_pin, arguments.name)
    elif arguments.branch_command == "resume":
        branch_result = resume_branch(git, path, target_pin, arguments.name)
    else:
        branch_result = publish_check(git, path, target_pin)
    return _logical_branch_result(branch_result, command, arguments.repo)


def _select_repository(facts, logical_path: str):
    """按逻辑路径返回实际仓库路径与目标 pin。"""
    if logical_path == ".":
        return facts.parent.path, facts.target_parent
    for repository in facts.repositories:
        if repository.path == logical_path:
            return repository.facts.path, repository.target_pin
    return None


def _logical_branch_result(result: Result, command: str, logical_path: str) -> Result:
    """将 branch 服务的物理路径收敛为公开逻辑仓路径。"""
    repositories = tuple(
        {
            **repository,
            "path": logical_path,
            "role": "parent" if logical_path == "." else "submodule",
        }
        for repository in result.repositories
    )
    actions = tuple(
        replace(action, argv=_logical_repo_argv(action.argv, logical_path))
        for action in result.next_actions
    )
    return replace(
        result, command=command, repositories=repositories, next_actions=actions
    )


def _logical_repo_argv(argv, logical_path: str):
    """将 branch 恢复动作中的物理仓库参数转换为公开逻辑路径。"""
    values = list(argv)
    if "--repo" in values:
        position = values.index("--repo") + 1
        if position < len(values):
            values[position] = logical_path
    return tuple(values)


def _audit(store: StateStore, result: Result) -> None:
    """严格按 latest、event 顺序持久一次命令结果。"""
    store.write_latest(result)
    store.append_event(result)


def _best_effort_failure_audit(store: StateStore, result: Result) -> None:
    """审计路径失败后尽力保存明确的 failed 结果。"""
    try:
        store.write_latest(result)
        store.append_event(result)
    except _EXPECTED_ERRORS:
        pass


def _domain_write_completed(result: Result) -> bool:
    """根据服务结果判断本命令是否已经越过首次领域写入边界。"""
    if result.state == "partial":
        return True
    return result.changed and result.command in (
        "sync apply",
        "branch start",
        "branch resume",
    )


def _persistence_failure(result: Result, reason: str) -> Result:
    """保留写后实际状态；写前失败返回不携带误导性成功数据的结果。"""
    if not _domain_write_completed(result):
        return _empty_result(result.command, "failed", (reason,))
    reasons = result.reason_codes
    if reason not in reasons:
        reasons += (reason,)
    return replace(result, state="partial", reason_codes=reasons, changed=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """解析命令行并返回稳定进程退出码。"""
    arguments = _build_parser().parse_args(argv)
    command = _command_name(arguments)
    output_format = getattr(arguments, "output_format", "text")
    config = None
    store = None
    try:
        config_path = Path(arguments.config) if hasattr(arguments, "config") else None
        config = load_config(_configuration_arguments(arguments), os.environ, config_path)
        store = StateStore(config.state_dir)
        with store.lock():
            if arguments.root_command != "init" and not config.config_path.exists():
                current = _empty_result(command, "unconfigured", ("config_missing",))
            else:
                try:
                    current = _dispatch(arguments, config, store, Git())
                except _EXPECTED_ERRORS:
                    current = _empty_result(command, "failed", ("command_failed",))
            try:
                _audit(store, current)
            except _EXPECTED_ERRORS:
                current = _persistence_failure(current, "audit_write_failed")
                _best_effort_failure_audit(store, current)
            try:
                _render(current, output_format)
            except _EXPECTED_ERRORS:
                current = _persistence_failure(current, "render_failed")
                _best_effort_failure_audit(store, current)
                return _exit_code(current)
            return _exit_code(current)
    except _EXPECTED_ERRORS:
        current = _empty_result(command, "failed", ("command_failed",))
        try:
            _render(current, output_format)
        except _EXPECTED_ERRORS:
            pass
        return 3

"""组合仓同步计划的复检、受控写入与部分状态报告。"""

import hashlib
import os
import stat
from pathlib import Path
from typing import Optional

from data_infra_sync.git import GitError
from data_infra_sync.model import Action, Result
from data_infra_sync.planner import plan_sync


def execute_sync(
    git,
    adapter,
    expected_snapshot: Optional[str],
    non_interactive: bool,
) -> Result:
    """fresh 复检同步计划，并按确定顺序应用可证明安全的组合更新。"""
    if (expected_snapshot is None) == (not non_interactive):
        return _failed(None, "invalid_apply_mode")

    try:
        facts = adapter.collect_plan_facts(git, fresh=True)
    except GitError:
        return _failed(None, "git_precondition_failed")

    plan = plan_sync(facts)
    if expected_snapshot is not None and expected_snapshot != plan.snapshot:
        return _from_plan(plan, "blocked", ("snapshot_mismatch",))
    if "managed_patch_transition_required" in plan.reason_codes:
        return _from_plan(
            plan, "blocked", ("managed_patch_transition_required",)
        )
    if plan.state not in ("up_to_date", "update_ready"):
        return _from_plan(plan, plan.state, plan.reason_codes)

    try:
        current_patches = adapter.managed_patches(facts.current_parent)
        target_patches = adapter.managed_patches(facts.target_parent)
    except GitError:
        return _failed(plan, "git_precondition_failed")
    if not _continuous_declarations(current_patches, target_patches):
        return _from_plan(
            plan, "blocked", ("managed_patch_transition_required",)
        )
    try:
        preflight_ok = adapter.preflight_managed_patches(
            git, facts, current_patches, target_patches
        )
    except GitError:
        return _failed(plan, "git_precondition_failed")
    if not preflight_ok:
        return _from_plan(
            plan, "blocked", ("managed_patch_transition_required",)
        )
    if plan.state == "up_to_date":
        try:
            all_applied = all(
                adapter.patch_state(git, patch) == "applied"
                for patch in target_patches
            )
        except GitError:
            return _failed(plan, "git_precondition_failed")
        if all_applied:
            return _from_plan(plan, "updated", (), changed=False)

    writes_started = False
    write_attempted = False
    failure_reason = "sync_write_failed"
    before_fingerprint = _domain_fingerprint(git, adapter, facts)
    try:
        # partial 重入时父仓已到目标；此时补丁已暂停，无需再次 reverse。
        if facts.current_parent != facts.target_parent:
            for patch in reversed(current_patches):
                failure_reason = "managed_patch_reverse_failed"
                state = adapter.patch_state(git, patch)
                if state == "applied":
                    write_attempted = True
                    adapter.reverse_patch(git, patch)
                    writes_started = True
                elif state != "absent":
                    raise _PatchTransitionError()

            failure_reason = "parent_update_failed"
            write_attempted = True
            git.run(
                facts.parent.path,
                ("merge", "--ff-only", facts.target_parent),
            )
            writes_started = True

        repositories = {item.path: item for item in facts.repositories}
        for target in sorted(facts.target_submodules, key=lambda item: item.path):
            repository = repositories[target.path]
            if repository.facts.head == target.pin:
                continue
            failure_reason = "submodule_update_failed"
            write_attempted = True
            if repository.facts.worktree == "missing":
                git.run(
                    facts.parent.path,
                    ("submodule", "update", "--init", "--checkout", "--", target.path),
                )
            else:
                git.run(
                    adapter.root / target.path,
                    ("checkout", "--detach", target.pin),
                )
            writes_started = True

        for patch in target_patches:
            failure_reason = "managed_patch_apply_failed"
            state = adapter.patch_state(git, patch)
            if state == "absent":
                write_attempted = True
                adapter.apply_patch(git, patch)
                writes_started = True
            elif state != "applied":
                raise _PatchTransitionError()

        failure_reason = "postcondition_failed"
        post_facts = adapter.collect_plan_facts(git, fresh=False)
        post_plan = plan_sync(post_facts)
        if post_plan.state != "up_to_date":
            return _partial(_actual_from_plan(post_plan), failure_reason)
        return _from_plan(post_plan, "updated", (), changed=True)
    except GitError:
        actual = _read_actual_state(git, adapter, facts, plan)
        after_fingerprint = _domain_fingerprint(git, adapter, facts)
        proven_unchanged = (
            before_fingerprint is not None
            and after_fingerprint is not None
            and before_fingerprint == after_fingerprint
        )
        if not writes_started and (not write_attempted or proven_unchanged):
            return _failed(plan, failure_reason)
        return _partial(actual, failure_reason)
    except _PatchTransitionError:
        if not writes_started:
            return _from_plan(
                plan, "blocked", ("managed_patch_transition_required",)
            )
        return _partial(
            _read_actual_state(git, adapter, facts, plan),
            "managed_patch_transition_required",
        )


class _PatchTransitionError(RuntimeError):
    """表示执行期间补丁状态偏离已完成的写前预检。"""


def _continuous_declarations(current, target) -> bool:
    """确认补丁数量、名称、字节哈希、目标子仓和适用路径完全相同。"""
    current_keys = tuple(_patch_key(item) for item in current)
    target_keys = tuple(_patch_key(item) for item in target)
    current_names = tuple(item[0] for item in current_keys)
    target_names = tuple(item[0] for item in target_keys)
    return (
        len(current_names) == len(set(current_names))
        and len(target_names) == len(set(target_names))
        and current_keys == target_keys
    )


def _patch_key(patch):
    """返回补丁连续性比较所需的稳定声明字段。"""
    return (
        patch.name,
        patch.content_hash,
        patch.target_submodule,
        patch.apply_path,
    )


def _read_actual_state(git, adapter, facts, before_plan):
    """失败后读取实际状态；完整收集失败时逐仓读取且不复用旧 HEAD。"""
    try:
        actual_facts = adapter.collect_plan_facts(git, fresh=False)
        actual_plan = plan_sync(actual_facts)
        actual = _actual_from_plan(actual_plan)
        return actual
    except GitError:
        return _read_repositories_individually(git, adapter, facts, before_plan)


def _read_repositories_individually(git, adapter, facts, before_plan):
    """逐仓尽力读取实际 Git 事实，并显式标记不可读取项。"""
    repositories = []
    changed = False
    read_failed = False
    for previous in before_plan.repositories:
        logical_path = previous["path"]
        path = facts.parent.path if logical_path == "." else adapter.root / logical_path
        try:
            head = git.run(path, ("rev-parse", "HEAD")).stdout.strip() or None
        except (GitError, OSError):
            head = None
        try:
            observed = git.inspect_repo(path)
        except (GitError, OSError):
            read_failed = True
            item = _repository_with_unread_auxiliary(
                git, path, previous, head
            )
            repositories.append(item)
            changed = changed or head != previous["head"]
            continue

        if head is None:
            head = observed.head
        item, item_read_failed = _observed_repository(
            git, path, logical_path, previous, observed, head
        )
        read_failed = read_failed or item_read_failed
        changed = changed or any(
            item[key] != previous[key]
            for key in ("head", "branch", "worktree")
        )
        repositories.append(
            item if item_read_failed else _progress_repository(item)
        )
    return {
        "target": before_plan.target,
        "repositories": tuple(repositories),
        "snapshot": None,
        "stale_target": False,
        "read_failed": read_failed,
        "changed": None if read_failed else changed,
    }


def _repository_with_unread_auxiliary(git, path, previous, head):
    """保留独立读取的 HEAD，并显式降级不可读辅助字段。"""
    relation = "not_applicable"
    if head is not None and previous["target_pin"] is not None:
        try:
            relation = git.relation(path, head, previous["target_pin"])
        except GitError:
            pass
    item = dict(previous)
    item.update(
        {
            "head": head,
            "branch": None,
            "upstream": None,
            "ahead": None,
            "behind": None,
            "worktree": "missing",
            "relation": relation,
            "reason_codes": ["actual_state_read_failed"],
        }
    )
    return item


def _observed_repository(git, path, logical_path, previous, observed, head):
    """将逐仓读取结果转换为协议 repository 对象。"""
    target_pin = previous["target_pin"]
    relation = "not_applicable"
    reason_codes = []
    if head is not None and target_pin is not None:
        try:
            relation = git.relation(path, head, target_pin)
        except GitError:
            reason_codes.append("actual_state_read_failed")
    return (
        {
            "path": logical_path,
            "role": previous["role"],
            "head": head,
            "target_pin": target_pin,
            "branch": observed.branch,
            "upstream": observed.upstream,
            "ahead": observed.ahead,
            "behind": observed.behind,
            "worktree": observed.worktree,
            "relation": relation,
            "reason_codes": reason_codes,
        },
        bool(reason_codes),
    )


def _domain_fingerprint(git, adapter, facts):
    """精确读取本地 refs、index 和相关工作树内容；任一失败返回 None。"""
    injected = getattr(git, "domain_fingerprint", None)
    if injected is not None:
        return injected(adapter, facts)
    repositories = [(".", facts.parent.path)]
    repositories.extend(
        (item.path, adapter.root / item.path)
        for item in sorted(facts.repositories, key=lambda item: item.path)
    )
    try:
        return tuple(
            (logical_path, _repository_fingerprint(git, Path(path)))
            for logical_path, path in repositories
        )
    except (GitError, OSError, RuntimeError, ValueError):
        return None


def _repository_fingerprint(git, repository):
    """返回单仓 refs、index、status 和 tracked/untracked 文件内容指纹。"""
    repository = Path(os.path.abspath(str(repository)))
    if not _safe_repository_path(repository):
        return ("missing",)
    head = git.run(repository, ("rev-parse", "HEAD")).stdout.strip()
    branch = git.run(
        repository, ("symbolic-ref", "--quiet", "HEAD"), check=False
    )
    refs = git.run(
        repository,
        ("for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads"),
    ).stdout
    index = git.run(repository, ("ls-files", "--stage", "-z")).stdout
    status = git.run(
        repository,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
    ).stdout
    paths = git.run(
        repository,
        ("ls-files", "-z", "--cached", "--others", "--exclude-standard"),
    ).stdout
    files = tuple(
        (relative, _path_fingerprint(repository, relative))
        for relative in sorted(item for item in paths.split("\0") if item)
    )
    return (
        head,
        branch.returncode,
        branch.stdout,
        refs,
        index,
        status,
        files,
    )


def _safe_repository_path(repository):
    """通过 lstat 验证仓库入口及其路径组件，拒绝任何 symlink。"""
    current = Path(repository.anchor)
    metadata = current.lstat()
    for part in repository.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("repository path contains symlink")
        if current != repository and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("repository path component is not a directory")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("repository path is not a directory")
    return True


def _path_fingerprint(repository, relative):
    """不跟随 symlink 地计算单个工作树路径的稳定内容指纹。"""
    path = repository
    parts = Path(relative).parts
    for position, part in enumerate(parts):
        path = path / part
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return (
                "symlink",
                position,
                stat.S_IMODE(metadata.st_mode),
                os.readlink(path),
            )
    if stat.S_ISREG(metadata.st_mode):
        return (
            "file",
            stat.S_IMODE(metadata.st_mode),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    if stat.S_ISDIR(metadata.st_mode):
        return ("directory", stat.S_IMODE(metadata.st_mode))
    return ("other", metadata.st_mode, metadata.st_size)


def _actual_from_plan(plan):
    """将已成功收集的实际计划转换为 partial 输入。"""
    return {
        "target": plan.target,
        "repositories": tuple(
            _progress_repository(item) for item in plan.repositories
        ),
        "snapshot": plan.snapshot,
        "stale_target": plan.stale_target,
        "read_failed": False,
        "changed": True,
    }


def _partial(actual, reason):
    """返回包含实际完成项、未完成项和直接恢复 argv 的 partial 结果。"""
    reasons = (reason,)
    if actual["read_failed"]:
        reasons += ("actual_state_read_failed",)
    action = Action(
        "resume_sync",
        ("data-infra-sync", "sync", "apply", "--non-interactive"),
        True,
        False,
        ("fresh_fetch", "full_recheck"),
    )
    return Result(
        "sync apply",
        "partial",
        reasons,
        actual["target"],
        actual["repositories"],
        True,
        (action,),
        actual["snapshot"],
        actual["stale_target"],
    )


def _progress_repository(repository):
    """用目标 pin 与实际 HEAD 标记已完成项和未完成项。"""
    item = dict(repository)
    completed = item["target_pin"] is not None and item["head"] == item["target_pin"]
    item["reason_codes"] = ["updated" if completed else "update_pending"]
    return item


def _from_plan(plan, state, reasons, *, changed=False):
    """保留 fresh 计划细节并转换为 sync apply 命令结果。"""
    return Result(
        "sync apply",
        state,
        tuple(reasons),
        plan.target,
        plan.repositories,
        changed,
        (),
        plan.snapshot,
        plan.stale_target,
    )


def _failed(plan, reason):
    """构造首次领域写入前的失败结果。"""
    if plan is None:
        return Result(
            "sync apply", "failed", (reason,), None, (), False, (), None, False
        )
    return _from_plan(plan, "failed", (reason,))

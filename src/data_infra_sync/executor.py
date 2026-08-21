"""组合仓同步计划的复检、受控写入与部分状态报告。"""

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
    if plan.state == "up_to_date":
        return _from_plan(plan, "updated", (), changed=False)
    if plan.state != "update_ready":
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

    writes_started = False
    failure_reason = "sync_write_failed"
    try:
        # partial 重入时父仓已到目标；此时补丁已暂停，无需再次 reverse。
        if facts.current_parent != facts.target_parent:
            for patch in current_patches:
                failure_reason = "managed_patch_reverse_failed"
                adapter.reverse_patch(git, patch)
                writes_started = True

            failure_reason = "parent_update_failed"
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
            adapter.apply_patch(git, patch)
            writes_started = True

        failure_reason = "postcondition_failed"
        post_facts = adapter.collect_plan_facts(git, fresh=False)
        post_plan = plan_sync(post_facts)
        if post_plan.state != "up_to_date":
            return _partial(post_plan, failure_reason)
        return _from_plan(post_plan, "updated", (), changed=True)
    except GitError:
        if not writes_started:
            return _failed(plan, failure_reason)
        return _partial(_actual_plan(git, adapter, plan), failure_reason)


def _continuous_declarations(current, target) -> bool:
    """确认补丁数量、名称、字节哈希、目标子仓和适用路径完全相同。"""
    current_keys = tuple(sorted((_patch_key(item) for item in current)))
    target_keys = tuple(sorted((_patch_key(item) for item in target)))
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


def _actual_plan(git, adapter, fallback):
    """失败后尽力重新读取实际 HEAD；读取失败时保留最后可信事实。"""
    try:
        return plan_sync(adapter.collect_plan_facts(git, fresh=False))
    except GitError:
        return fallback


def _partial(plan, reason):
    """返回包含实际完成项、未完成项和直接恢复 argv 的 partial 结果。"""
    repositories = tuple(_progress_repository(item) for item in plan.repositories)
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
        (reason,),
        plan.target,
        repositories,
        True,
        (action,),
        plan.snapshot,
        False,
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

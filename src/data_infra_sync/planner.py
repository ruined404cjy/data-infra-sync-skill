"""根据不可变 Git 事实计算组合仓同步计划。"""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Optional, Tuple

from data_infra_sync.git import RepoFacts
from data_infra_sync.model import Action, Result


Relation = Literal["equal", "contained", "tree_equal", "diverged", "not_applicable"]
ManagedPatchState = Literal["none", "continuous", "transition"]

_REASON_ORDER = (
    "target_parent_missing",
    "parent_branch_mismatch",
    "parent_not_fast_forward",
    "dirty_worktree",
    "active_git_operation",
    "running_instances",
    "unsupported_nested_submodule",
    "target_pin_missing",
    "target_pin_does_not_cover_head",
    "submodule_layout_transition_required",
    "managed_patch_transition_required",
)


@dataclass(frozen=True)
class SubmoduleSpec:
    """描述父仓声明的 submodule 身份与精确 pin。"""

    name: str
    path: str
    url: str
    pin: str


@dataclass(frozen=True)
class RepositoryPlanFacts:
    """描述单个逻辑子仓计算同步计划所需的事实。"""

    path: str
    facts: RepoFacts
    current_pin: Optional[str]
    target_pin: Optional[str]
    relation: Relation
    managed_patch_state: ManagedPatchState


@dataclass(frozen=True)
class PlanFacts:
    """描述一次纯同步计划的全部不可变输入。"""

    parent: RepoFacts
    current_parent: str
    target_parent: Optional[str]
    target_remote: str
    target_branch: str
    required_parent_branch: str
    parent_relation: Relation
    parent_non_submodule_dirty: bool
    current_submodules: tuple[SubmoduleSpec, ...]
    target_submodules: tuple[SubmoduleSpec, ...]
    repositories: tuple[RepositoryPlanFacts, ...]
    running_instances: bool
    nested_submodules: bool
    stale_target: bool = False
    managed_patch_transition: bool = False


def plan_sync(facts: PlanFacts) -> Result:
    """根据给定事实返回稳定状态、原因、后续操作和 snapshot。"""
    if not _valid_repository_paths(facts):
        return Result(
            "sync plan",
            "failed",
            ("invalid_plan_facts",),
            _target(facts),
            (),
            False,
            (),
            None,
            facts.stale_target,
        )

    repository_reasons = {
        repository.path: _repository_reasons(repository)
        for repository in facts.repositories
    }
    parent_reasons = _parent_reasons(facts)
    reasons = set(parent_reasons)
    for items in repository_reasons.values():
        reasons.update(items)
    if facts.running_instances:
        reasons.add("running_instances")
    if facts.nested_submodules:
        reasons.add("unsupported_nested_submodule")
    if facts.managed_patch_transition:
        reasons.add("managed_patch_transition_required")
    if facts.target_parent is not None and _layout_changed(facts):
        reasons.add("submodule_layout_transition_required")

    ordered_reasons = tuple(reason for reason in _REASON_ORDER if reason in reasons)
    state = _state(facts, ordered_reasons)
    snapshot = snapshot_for(facts)
    actions: Tuple[Action, ...] = ()
    if state == "update_ready":
        if facts.stale_target:
            actions = (
                Action(
                    "sync_plan",
                    ("data-infra-sync", "sync", "plan"),
                    False,
                    False,
                    (),
                ),
            )
        else:
            actions = (
                Action(
                    "sync_apply",
                    ("data-infra-sync", "sync", "apply", "--snapshot", snapshot),
                    True,
                    False,
                    ("fresh_fetch", "snapshot_matches"),
                ),
            )

    return Result(
        "sync plan",
        state,
        ordered_reasons,
        _target(facts),
        _repositories(facts, parent_reasons, repository_reasons),
        False,
        actions,
        snapshot,
        facts.stale_target,
    )


def snapshot_for(facts: PlanFacts) -> str:
    """对排序后的执行前置事实规范 JSON 计算 SHA-256。"""
    document = {
        "target": {
            "remote": facts.target_remote,
            "branch": facts.target_branch,
            "parent": facts.target_parent,
            "gitlinks": {
                item.path: item.pin
                for item in sorted(facts.target_submodules, key=lambda item: item.path)
            },
            "submodules": [
                _submodule_snapshot(item)
                for item in _sorted_specs(facts.target_submodules)
            ],
        },
        "parent": {
            "current_parent": facts.current_parent,
            "head": facts.parent.head,
            "branch": facts.parent.branch,
            "required_branch": facts.required_parent_branch,
            "relation": facts.parent_relation,
            "worktree": facts.parent.worktree,
            "index_dirty": facts.parent.index_dirty,
            "worktree_dirty": facts.parent.worktree_dirty,
            "non_submodule_dirty": facts.parent_non_submodule_dirty,
            "operation": facts.parent.operation,
        },
        "current_submodules": [
            _submodule_snapshot(item) for item in _sorted_specs(facts.current_submodules)
        ],
        "repositories": [
            _repository_snapshot(item)
            for item in sorted(facts.repositories, key=lambda item: item.path)
        ],
        "running_instances": facts.running_instances,
        "nested_submodules": facts.nested_submodules,
        "stale_target": facts.stale_target,
        "managed_patch_transition": facts.managed_patch_transition,
    }
    encoded = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parent_reasons(facts: PlanFacts) -> tuple[str, ...]:
    """返回父仓对应的计划原因。"""
    reasons = set()
    if facts.target_parent is None:
        reasons.add("target_parent_missing")
    if facts.parent.branch != facts.required_parent_branch:
        reasons.add("parent_branch_mismatch")
    if (
        facts.target_parent is not None
        and facts.parent_relation not in ("equal", "contained")
    ):
        reasons.add("parent_not_fast_forward")
    if facts.parent_non_submodule_dirty:
        reasons.add("dirty_worktree")
    if facts.parent.operation is not None:
        reasons.add("active_git_operation")
    return tuple(reason for reason in _REASON_ORDER if reason in reasons)


def _repository_reasons(repository: RepositoryPlanFacts) -> tuple[str, ...]:
    """返回单个子仓对应的计划原因。"""
    reasons = set()
    if (
        repository.facts.worktree == "dirty"
        and repository.managed_patch_state != "continuous"
    ):
        reasons.add("dirty_worktree")
    if repository.facts.operation is not None:
        reasons.add("active_git_operation")
    if repository.target_pin is None:
        reasons.add("target_pin_missing")
    elif repository.relation == "diverged":
        reasons.add("target_pin_does_not_cover_head")
    if repository.managed_patch_state == "transition":
        reasons.add("managed_patch_transition_required")
    return tuple(reason for reason in _REASON_ORDER if reason in reasons)


def _state(facts: PlanFacts, reasons: tuple[str, ...]) -> str:
    """按固定安全优先级选择同步状态。"""
    blocked_reasons = {
        "parent_branch_mismatch",
        "parent_not_fast_forward",
        "dirty_worktree",
        "active_git_operation",
        "running_instances",
        "unsupported_nested_submodule",
        "submodule_layout_transition_required",
        "managed_patch_transition_required",
    }
    if blocked_reasons.intersection(reasons):
        return "blocked"
    if "target_parent_missing" in reasons or "target_pin_missing" in reasons:
        return "waiting_for_pin"
    if "target_pin_does_not_cover_head" in reasons:
        return "waiting_for_pin"
    if _all_heads_equal(facts):
        return "up_to_date"
    return "update_ready"


def _layout_changed(facts: PlanFacts) -> bool:
    """判断现有 submodule 是否发生删除、改名、路径或 URL 变化。"""
    targets_by_path = {item.path: item for item in facts.target_submodules}
    current_names = {item.name for item in facts.current_submodules}
    for current in facts.current_submodules:
        target = targets_by_path.get(current.path)
        if target is None or target.name != current.name or target.url != current.url:
            return True
    current_paths = {item.path for item in facts.current_submodules}
    return any(
        target.path not in current_paths and target.name in current_names
        for target in facts.target_submodules
    )


def _valid_repository_paths(facts: PlanFacts) -> bool:
    """确认 repository 路径唯一且与 submodule 路径并集完全一致。"""
    repository_paths = [item.path for item in facts.repositories]
    if len(repository_paths) != len(set(repository_paths)):
        return False
    expected_paths = {item.path for item in facts.current_submodules}
    expected_paths.update(item.path for item in facts.target_submodules)
    return set(repository_paths) == expected_paths


def _all_heads_equal(facts: PlanFacts) -> bool:
    """确认父仓与全部目标 submodule HEAD 精确等于目标。"""
    if (
        facts.target_parent is None
        or facts.current_parent != facts.target_parent
        or facts.parent.head != facts.target_parent
    ):
        return False
    repositories = {item.path: item for item in facts.repositories}
    for target in facts.target_submodules:
        repository = repositories.get(target.path)
        if repository is None or repository.facts.head != target.pin:
            return False
    return True


def _target(facts: PlanFacts):
    """构造 Result 的目标对象。"""
    if facts.target_parent is None:
        return None
    return {
        "parent_commit": facts.target_parent,
        "remote": facts.target_remote,
        "branch": facts.target_branch,
        "gitlinks": {
            item.path: item.pin
            for item in sorted(facts.target_submodules, key=lambda item: item.path)
        },
    }


def _repositories(facts, parent_reasons, repository_reasons):
    """构造按逻辑路径排序且不含绝对路径的仓库结果。"""
    parent = _repository_dict(
        ".",
        "parent",
        facts.parent,
        facts.target_parent,
        facts.parent_relation,
        parent_reasons,
    )
    children = tuple(
        _repository_dict(
            item.path,
            "submodule",
            item.facts,
            item.target_pin,
            item.relation,
            repository_reasons[item.path],
        )
        for item in sorted(facts.repositories, key=lambda item: item.path)
    )
    return (parent,) + children


def _repository_dict(path, role, facts, target_pin, relation, reasons):
    """将仓库事实转换为 Result repository 对象。"""
    return {
        "path": path,
        "role": role,
        "head": facts.head,
        "target_pin": target_pin,
        "branch": facts.branch,
        "upstream": facts.upstream,
        "ahead": facts.ahead,
        "behind": facts.behind,
        "worktree": facts.worktree,
        "relation": relation,
        "reason_codes": list(reasons),
    }


def _sorted_specs(specs):
    """按逻辑路径、名称和 URL 规范排序 submodule 声明。"""
    return sorted(specs, key=lambda item: (item.path, item.name, item.url, item.pin))


def _submodule_snapshot(item):
    """构造不含本地绝对路径的 submodule snapshot 对象。"""
    return {"name": item.name, "path": item.path, "url": item.url, "pin": item.pin}


def _repository_snapshot(item):
    """构造单个逻辑子仓的执行前置事实。"""
    return {
        "path": item.path,
        "head": item.facts.head,
        "worktree": item.facts.worktree,
        "index_dirty": item.facts.index_dirty,
        "worktree_dirty": item.facts.worktree_dirty,
        "branch": item.facts.branch,
        "upstream": item.facts.upstream,
        "ahead": item.facts.ahead,
        "behind": item.facts.behind,
        "operation": item.facts.operation,
        "current_pin": item.current_pin,
        "target_pin": item.target_pin,
        "relation": item.relation,
        "managed_patch_state": item.managed_patch_state,
    }

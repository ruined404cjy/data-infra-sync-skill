"""开发分支的状态检查、创建、恢复和发布前检查。"""

from pathlib import Path
from typing import Optional, Tuple

from data_infra_sync.git import Git, GitError, RepoFacts
from data_infra_sync.model import Result


_COVERED_RELATIONS = frozenset(("equal", "contained", "tree_equal"))
_CONTAINED_RELATIONS = frozenset(("equal", "contained"))


def branch_status(git: Git, repo: Path, target_pin: str) -> Result:
    """返回本地分支、upstream 与目标 pin 的关系。"""
    facts = git.inspect_repo(repo)
    target_exists = _target_exists(git, repo, target_pin)
    relation = _relation(git, repo, facts, target_pin, target_exists)
    if not target_exists:
        return _result(
            "branch status", "waiting_for_pin", ("target_pin_missing",), facts, target_pin, relation
        )

    reasons = []
    if facts.upstream is None:
        reasons.append("upstream_missing")
    if relation not in _COVERED_RELATIONS:
        reasons.append(_coverage_reason(facts))
    return _result("branch status", "branch_status", tuple(reasons), facts, target_pin, relation)


def start_branch(git: Git, repo: Path, target_pin: str, name: str) -> Result:
    """从目标 pin 创建并切换到新的本地开发分支。"""
    facts, relation, blocked = _prepare_switch(git, repo, target_pin)
    if blocked is not None:
        return _precondition_result("branch start", blocked, facts, target_pin, relation)
    if not _valid_branch_name(git, repo, name):
        return _result(
            "branch start", "blocked", ("invalid_branch_name",), facts, target_pin, relation
        )
    if _local_branch_exists(git, repo, name):
        return _result("branch start", "blocked", ("branch_exists",), facts, target_pin, relation)

    git.run(repo, ("switch", "-c", name, target_pin))
    updated = git.inspect_repo(repo)
    return _result("branch start", "branch_started", (), updated, target_pin, "equal", changed=True)


def resume_branch(git: Git, repo: Path, target_pin: str, name: str) -> Result:
    """切换到指定且已存在的本地开发分支。"""
    facts, relation, blocked = _prepare_switch(git, repo, target_pin)
    if blocked is not None:
        return _precondition_result("branch resume", blocked, facts, target_pin, relation)
    if not _valid_branch_name(git, repo, name):
        return _result(
            "branch resume", "blocked", ("invalid_branch_name",), facts, target_pin, relation
        )
    if not _local_branch_exists(git, repo, name):
        return _result("branch resume", "blocked", ("branch_missing",), facts, target_pin, relation)
    if facts.branch == name:
        return _result("branch resume", "branch_resumed", (), facts, target_pin, relation)

    git.run(repo, ("switch", name))
    updated = git.inspect_repo(repo)
    updated_relation = _relation(git, repo, updated, target_pin, True)
    return _result(
        "branch resume", "branch_resumed", (), updated, target_pin, updated_relation, changed=True
    )


def publish_check(git: Git, repo: Path, target_pin: str) -> Result:
    """fresh fetch 后检查 upstream 发布与目标 pin 覆盖状态。"""
    facts = git.inspect_repo(repo)
    if facts.worktree != "clean":
        return _result(
            "branch publish-check",
            "blocked",
            ("dirty_worktree",),
            facts,
            target_pin,
            "not_applicable",
        )
    if facts.operation is not None:
        return _result(
            "branch publish-check",
            "blocked",
            ("active_git_operation",),
            facts,
            target_pin,
            "not_applicable",
        )
    if facts.upstream is None:
        return _result(
            "branch publish-check",
            "blocked",
            ("upstream_missing",),
            facts,
            target_pin,
            "not_applicable",
        )

    git.run(repo, ("fetch",))
    fresh = git.inspect_repo(repo)
    if not _target_exists(git, repo, target_pin):
        return _result(
            "branch publish-check",
            "waiting_for_pin",
            ("target_pin_missing",),
            fresh,
            target_pin,
            "not_applicable",
        )
    fresh_relation = _relation(git, repo, fresh, target_pin, True)
    upstream_relation = git.relation(repo, fresh.head, fresh.upstream)
    if upstream_relation not in _CONTAINED_RELATIONS:
        return _result(
            "branch publish-check",
            "publish_required",
            ("unpushed_commits",),
            fresh,
            target_pin,
            fresh_relation,
        )
    if fresh_relation not in _COVERED_RELATIONS:
        return _result(
            "branch publish-check",
            "waiting_for_pin",
            ("target_pin_does_not_cover_head",),
            fresh,
            target_pin,
            fresh_relation,
        )
    return _result(
        "branch publish-check", "publish_verified", (), fresh, target_pin, fresh_relation
    )


def _prepare_switch(
    git: Git, repo: Path, target_pin: str
) -> Tuple[RepoFacts, str, Optional[str]]:
    """读取离开当前 HEAD 的安全前置条件。"""
    facts = git.inspect_repo(repo)
    if not _target_exists(git, repo, target_pin):
        return facts, "not_applicable", "target_pin_missing"
    relation = _relation(git, repo, facts, target_pin, True)
    if facts.worktree != "clean":
        return facts, relation, "dirty_worktree"
    if facts.operation is not None:
        return facts, relation, "active_git_operation"
    if relation not in _COVERED_RELATIONS:
        return facts, relation, _coverage_reason(facts)
    return facts, relation, None


def _target_exists(git: Git, repo: Path, target_pin: str) -> bool:
    """确认目标 pin 已存在于本地对象库。"""
    completed = git.run(
        repo,
        ("rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(target_pin)),
        check=False,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise GitError(
        tuple(str(item) for item in completed.args), completed.stderr, completed.returncode
    )


def _valid_branch_name(git: Git, repo: Path, name: str) -> bool:
    """使用 Git 的分支引用规则验证用户输入。"""
    return git.run(repo, ("check-ref-format", "--branch", name), check=False).returncode == 0


def _local_branch_exists(git: Git, repo: Path, name: str) -> bool:
    """确认指定名称对应本地分支引用。"""
    completed = git.run(
        repo,
        ("show-ref", "--verify", "--quiet", "refs/heads/{}".format(name)),
        check=False,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise GitError(
        tuple(str(item) for item in completed.args), completed.stderr, completed.returncode
    )


def _relation(git: Git, repo: Path, facts: RepoFacts, target_pin: str, target_exists: bool) -> str:
    """返回当前 HEAD 相对目标 pin 的覆盖关系。"""
    if not target_exists or facts.head is None:
        return "not_applicable"
    return git.relation(repo, facts.head, target_pin)


def _coverage_reason(facts: RepoFacts) -> str:
    """根据 HEAD 形式返回未被目标覆盖的阻塞原因。"""
    if facts.branch is None:
        return "detached_head_not_covered"
    return "current_head_not_covered"


def _precondition_result(
    command: str, reason: str, facts: RepoFacts, target_pin: str, relation: str
) -> Result:
    """返回目标缺失或安全前置条件不满足的结果。"""
    state = "waiting_for_pin" if reason == "target_pin_missing" else "blocked"
    return _result(command, state, (reason,), facts, target_pin, relation)


def _result(
    command: str,
    state: str,
    reason_codes: Tuple[str, ...],
    facts: RepoFacts,
    target_pin: str,
    relation: str,
    *,
    changed: bool = False,
) -> Result:
    """将 Git 事实转换为符合稳定 schema 的分支命令结果。"""
    repository = {
        "path": str(facts.path),
        "role": "submodule",
        "head": facts.head,
        "target_pin": target_pin,
        "branch": facts.branch,
        "upstream": facts.upstream,
        "ahead": facts.ahead,
        "behind": facts.behind,
        "worktree": facts.worktree,
        "relation": relation,
        "reason_codes": list(reason_codes),
    }
    return Result(command, state, reason_codes, None, (repository,), changed, (), None, None)

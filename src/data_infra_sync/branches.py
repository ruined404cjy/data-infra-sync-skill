"""开发分支的状态检查、创建、恢复和发布前检查。"""

from dataclasses import replace
from pathlib import Path
from typing import Optional, Tuple

from data_infra_sync.fingerprint import repository_fingerprint
from data_infra_sync.git import Git, GitError, RepoFacts
from data_infra_sync.model import Action, Result


_COVERED_RELATIONS = frozenset(("equal", "contained", "tree_equal"))
_CONTAINED_RELATIONS = frozenset(("equal", "contained"))
_EXPECTED_BRANCH_ERRORS = (GitError, OSError, RuntimeError, ValueError)


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

    before_fingerprint = _switch_fingerprint(git, repo)
    try:
        git.run(repo, ("switch", "-c", name, target_pin))
    except _EXPECTED_BRANCH_ERRORS:
        after_fingerprint = _switch_fingerprint(git, repo)
        identity = _read_branch_identity(git, repo)
        partial = _branch_postcondition_failed(
            git, repo, facts, target_pin, "branch start", identity, name
        )
        if (
            before_fingerprint is not None
            and after_fingerprint is not None
            and before_fingerprint == after_fingerprint
        ):
            raise
        return partial
    try:
        updated = git.inspect_repo(repo)
    except _EXPECTED_BRANCH_ERRORS:
        return _branch_postcondition_failed(
            git, repo, facts, target_pin, "branch start", requested_name=name
        )
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

    before_fingerprint = _switch_fingerprint(git, repo)
    try:
        git.run(repo, ("switch", name))
    except _EXPECTED_BRANCH_ERRORS:
        after_fingerprint = _switch_fingerprint(git, repo)
        identity = _read_branch_identity(git, repo)
        partial = _branch_postcondition_failed(
            git, repo, facts, target_pin, "branch resume", identity, name
        )
        if (
            before_fingerprint is not None
            and after_fingerprint is not None
            and before_fingerprint == after_fingerprint
        ):
            raise
        return partial
    try:
        updated = git.inspect_repo(repo)
        updated_relation = _relation(git, repo, updated, target_pin, True)
    except _EXPECTED_BRANCH_ERRORS:
        return _branch_postcondition_failed(
            git, repo, facts, target_pin, "branch resume", requested_name=name
        )
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
    if facts.upstream is None and not _branch_upstream_is_configured(git, repo, facts.branch):
        return _result(
            "branch publish-check",
            "blocked",
            ("upstream_missing",),
            facts,
            target_pin,
            "not_applicable",
        )

    _fetch_current_upstream(git, repo, facts.branch)
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


def _fetch_current_upstream(git: Git, repo: Path, branch: Optional[str]) -> None:
    """仅抓取当前分支 upstream 的源引用，并安全更新其 tracking 引用。"""
    if branch is None:
        raise GitError(("git", "fetch"), "current branch is missing", 2)
    local_branch = "refs/heads/{}".format(branch)
    _require_valid_ref(git, repo, local_branch)
    remote = git.run(
        repo, ("config", "--get", "branch.{}.remote".format(branch)), check=False
    )
    merge = git.run(
        repo, ("config", "--get", "branch.{}.merge".format(branch)), check=False
    )
    if remote.returncode != 0 or merge.returncode != 0:
        failed = remote if remote.returncode != 0 else merge
        raise GitError(
            tuple(str(item) for item in failed.args),
            failed.stderr or "current branch upstream is missing",
            failed.returncode,
        )
    remote_name = remote.stdout.strip()
    source = merge.stdout.strip()
    if not remote_name or remote_name == ".":
        raise GitError(("git", "fetch"), "unsafe local upstream", 2)
    if not source.startswith("refs/heads/"):
        raise GitError(("git", "fetch"), "unsafe upstream source", 2)
    _require_valid_ref(git, repo, source)
    upstream = git.run(
        repo,
        ("for-each-ref", "--format=%(upstream)", local_branch),
        check=False,
    )
    if upstream.returncode != 0:
        raise GitError(
            tuple(str(item) for item in upstream.args),
            upstream.stderr or "current branch upstream is missing",
            upstream.returncode,
        )
    destination = upstream.stdout.strip()
    if not destination.startswith("refs/remotes/"):
        raise GitError(("git", "fetch"), "unsafe upstream destination", 2)
    _require_valid_ref(git, repo, destination)
    git.run(
        repo,
        (
            "fetch",
            "--no-recurse-submodules",
            "--refmap=",
            "--",
            remote_name,
            source,
        ),
    )
    fetched = git.run(repo, ("rev-parse", "--verify", "FETCH_HEAD^{commit}"))
    commit = fetched.stdout.strip()
    if not commit:
        raise GitError(tuple(str(item) for item in fetched.args), "empty fetched commit", 2)
    git.run(repo, ("update-ref", "--no-deref", destination, commit))


def _branch_upstream_is_configured(git: Git, repo: Path, branch: Optional[str]) -> bool:
    """区分未配置 upstream 与配置存在但无法安全解析的情况。"""
    if branch is None:
        return False
    remote = git.run(
        repo, ("config", "--get", "branch.{}.remote".format(branch)), check=False
    )
    merge = git.run(
        repo, ("config", "--get", "branch.{}.merge".format(branch)), check=False
    )
    if remote.returncode == 1 and merge.returncode == 1:
        return False
    if remote.returncode not in (0, 1) or merge.returncode not in (0, 1):
        failed = remote if remote.returncode not in (0, 1) else merge
        raise GitError(
            tuple(str(item) for item in failed.args), failed.stderr, failed.returncode
        )
    return True


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


def _require_valid_ref(git: Git, repo: Path, ref: str) -> None:
    """要求引用通过 Git 的完整引用格式校验。"""
    completed = git.run(repo, ("check-ref-format", ref), check=False)
    if completed.returncode != 0:
        raise GitError(
            tuple(str(item) for item in completed.args),
            completed.stderr or "invalid ref",
            completed.returncode,
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


def _read_branch_identity(git, repo):
    """独立读取实际 HEAD/branch，并标记两项是否均可确认。"""
    readable = True
    head = None
    branch = None
    try:
        head = git.run(repo, ("rev-parse", "HEAD")).stdout.strip() or None
    except _EXPECTED_BRANCH_ERRORS:
        readable = False
    try:
        observed = git.run(
            repo, ("symbolic-ref", "--quiet", "--short", "HEAD"), check=False
        )
        if observed.returncode == 0:
            branch = observed.stdout.strip() or None
        elif observed.returncode != 1:
            readable = False
    except _EXPECTED_BRANCH_ERRORS:
        readable = False
    return head, branch, readable


def _switch_fingerprint(git, repo):
    """读取 switch 前后完整本地领域状态；失败时返回未知。"""
    try:
        return repository_fingerprint(git, repo)
    except _EXPECTED_BRANCH_ERRORS:
        return None


def _branch_postcondition_failed(
    git, repo, previous, target_pin, command, identity=None, requested_name=None
):
    """切换成功后尽力读取实际 HEAD/branch，并返回可恢复的 partial。"""
    head, branch, _ = identity or _read_branch_identity(git, repo)
    actual = replace(previous, head=head, branch=branch)
    result = _result(
        command,
        "partial",
        ("branch_postcondition_failed",),
        actual,
        target_pin,
        "not_applicable",
        changed=True,
    )
    action_argv = ["data-infra-sync", "branch", "status", "--repo", str(repo)]
    action_kind = "branch_status"
    if requested_name is not None:
        action_kind = "branch_resume"
        action_argv = [
            "data-infra-sync", "branch", "resume", "--repo", str(repo),
            "--name", requested_name,
        ]
    action = Action(
        action_kind,
        tuple(action_argv),
        requested_name is not None,
        False,
        ("full_recheck",),
    )
    return replace(result, next_actions=(action,))


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

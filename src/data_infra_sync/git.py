"""Git 子进程边界及只读组合仓事实采集。"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Literal, Optional, Sequence, Tuple


_URL_USERINFO = re.compile(r"((?:[a-z][a-z0-9+.-]*://))[^/@\s]+@", re.IGNORECASE)
_SENSITIVE_WORDS = (
    r"access_token|private_token|api_key|token|password|passwd|secret|credential|authorization|auth|key"
)
_URL_TOKEN = re.compile(r"([?&](?:" + _SENSITIVE_WORDS + r")=)[^&#\s]*", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(\b(?:" + _SENSITIVE_WORDS + r")\s*[:=]\s*)[^,\s&#]*", re.IGNORECASE
)
_OPERATION_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("REBASE_HEAD", "rebase"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("sequencer", "sequencer"),
    ("BISECT_LOG", "bisect"),
)
_GIT_REDIRECT_ENV = frozenset(
    (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    )
)
_GIT_CONFIG_ENTRY_ENV = re.compile(r"^GIT_CONFIG_(?:KEY|VALUE)_[0-9]+$")


@dataclass(frozen=True)
class Gitlink:
    """描述父仓树中一个一级 submodule 的精确提交。"""

    path: str
    commit: str


@dataclass(frozen=True)
class RepoFacts:
    """描述单个仓库当前的只读 Git 状态。"""

    path: Path
    head: Optional[str]
    branch: Optional[str]
    upstream: Optional[str]
    ahead: Optional[int]
    behind: Optional[int]
    worktree: Literal["clean", "dirty", "missing"]
    index_dirty: bool
    worktree_dirty: bool
    operation: Optional[str]


class GitError(RuntimeError):
    """Git 命令失败时提供已脱敏的 argv 与 stderr 摘要。"""

    def __init__(self, argv: Sequence[str], stderr: str, returncode: int):
        self.argv = tuple(_redact(argument) for argument in argv)
        self.stderr = _summarize(stderr)
        self.returncode = returncode
        super().__init__(
            "git command failed (exit {}): argv={!r}; stderr={}".format(
                returncode, self.argv, self.stderr
            )
        )


class Git:
    """统一执行 Git argv 命令并收集仓库事实。"""

    def run(
        self, repo: Path, args: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """在 repo 中执行 Git argv，并在失败时抛出脱敏 GitError。"""
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in _GIT_REDIRECT_ENV
            and _GIT_CONFIG_ENTRY_ENV.fullmatch(name) is None
        }
        environment["GIT_TERMINAL_PROMPT"] = "0"
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=environment,
        )
        if check and completed.returncode != 0:
            raise GitError(tuple(str(item) for item in completed.args), completed.stderr, completed.returncode)
        return completed

    def inspect_repo(self, path: Path) -> RepoFacts:
        """返回仓库 HEAD、分支、上游、脏状态和活动操作。"""
        resolved_path = path.resolve(strict=False)
        if not resolved_path.exists():
            return RepoFacts(
                resolved_path,
                None,
                None,
                None,
                None,
                None,
                "missing",
                False,
                False,
                None,
            )

        head = self.run(resolved_path, ("rev-parse", "HEAD")).stdout.strip()
        branch = self._optional_output(
            resolved_path, ("symbolic-ref", "--quiet", "--short", "HEAD")
        )
        upstream = self._upstream(resolved_path)
        ahead, behind = self._ahead_behind(resolved_path, upstream)
        index_dirty, worktree_dirty = self._status(resolved_path)
        return RepoFacts(
            resolved_path,
            head,
            branch,
            upstream,
            ahead,
            behind,
            "dirty" if index_dirty or worktree_dirty else "clean",
            index_dirty,
            worktree_dirty,
            self._operation(resolved_path),
        )

    def gitlinks(self, parent: Path, commit: str) -> Dict[str, Gitlink]:
        """解析 parent 指定提交树中的一级 gitlink。"""
        output = self.run(parent, ("ls-tree", "-r", "-z", commit)).stdout
        links: Dict[str, Gitlink] = {}
        for entry in output.split("\0"):
            if not entry:
                continue
            metadata, separator, path = entry.partition("\t")
            if not separator:
                continue
            mode, object_type, object_id = metadata.split(" ", 2)
            if mode == "160000" and object_type == "commit":
                links[path] = Gitlink(path, object_id)
        return links

    def relation(
        self, repo: Path, head: str, target: str
    ) -> Literal["equal", "contained", "tree_equal", "diverged"]:
        """判定 head 是否已被 target 覆盖、树等价或发生分叉。"""
        if head == target:
            return "equal"
        contained = self.run(
            repo, ("merge-base", "--is-ancestor", head, target), check=False
        )
        if contained.returncode == 0:
            return "contained"
        if contained.returncode != 1:
            raise GitError(
                tuple(str(item) for item in contained.args),
                contained.stderr,
                contained.returncode,
            )
        head_tree = self.run(repo, ("rev-parse", "{}^{{tree}}".format(head))).stdout.strip()
        target_tree = self.run(repo, ("rev-parse", "{}^{{tree}}".format(target))).stdout.strip()
        if head_tree == target_tree:
            return "tree_equal"
        return "diverged"

    def _optional_output(self, repo: Path, args: Sequence[str]) -> Optional[str]:
        """返回成功命令的非空输出，预期不存在时返回 None。"""
        completed = self.run(repo, args, check=False)
        if completed.returncode == 0:
            return completed.stdout.strip() or None
        if completed.returncode == 1:
            return None
        raise GitError(tuple(str(item) for item in completed.args), completed.stderr, completed.returncode)

    def _upstream(self, repo: Path) -> Optional[str]:
        """返回当前 HEAD 的 upstream，未配置或 detached 时返回 None。"""
        completed = self.run(
            repo,
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.strip() or None
        # 此专用 rev-parse 查询以 128 表示无 upstream 或 detached HEAD。
        if completed.returncode in (1, 128):
            return None
        raise GitError(tuple(str(item) for item in completed.args), completed.stderr, completed.returncode)

    def _ahead_behind(self, repo: Path, upstream: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
        """返回相对 upstream 的 ahead/behind 提交数。"""
        if upstream is None:
            return None, None
        completed = self.run(repo, ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"))
        ahead, behind = completed.stdout.split()
        return int(ahead), int(behind)

    def _status(self, repo: Path) -> Tuple[bool, bool]:
        """解析 NUL 分隔 porcelain 状态，分别返回 index 与工作树脏状态。"""
        output = self.run(
            repo, ("status", "--porcelain=v1", "-z", "--untracked-files=all")
        ).stdout
        index_dirty = False
        worktree_dirty = False
        for entry in output.split("\0"):
            if len(entry) < 3 or entry[2] != " ":
                continue
            index_status, worktree_status = entry[0], entry[1]
            index_dirty = index_dirty or index_status not in (" ", "?", "!")
            worktree_dirty = worktree_dirty or worktree_status not in (" ", "!")
        return index_dirty, worktree_dirty

    def _operation(self, repo: Path) -> Optional[str]:
        """返回正在进行的 Git 操作名称，未发现时返回 None。"""
        for marker, operation in _OPERATION_MARKERS:
            marker_path = Path(self.run(repo, ("rev-parse", "--git-path", marker)).stdout.strip())
            if not marker_path.is_absolute():
                marker_path = repo / marker_path
            if marker_path.exists():
                return operation
        return None


def gitlinks(parent: Path, commit: str) -> Dict[str, Gitlink]:
    """解析 parent 指定提交树中的一级 gitlink。"""
    return Git().gitlinks(parent, commit)


def relation(
    repo: Path, head: str, target: str
) -> Literal["equal", "contained", "tree_equal", "diverged"]:
    """判定 head 与 target 的覆盖和树关系。"""
    return Git().relation(repo, head, target)


def _redact(value: str) -> str:
    """移除 Git 诊断文本中的 URL 凭据和敏感赋值。"""
    redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", value)
    redacted = _URL_TOKEN.sub(r"\1[REDACTED]", redacted)
    return _SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", redacted)


def _summarize(stderr: str) -> str:
    """返回已脱敏且长度受限的 stderr 摘要。"""
    summary = _redact(stderr.strip().replace("\n", " "))
    if len(summary) > 500:
        return summary[:497] + "..."
    return summary or "<no stderr>"

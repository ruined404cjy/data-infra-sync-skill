"""DataInfra 项目的同步事实与受控补丁声明边界。"""

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Tuple

from data_infra_sync.git import GitError


@dataclass(frozen=True)
class ManagedPatch:
    """描述父仓版本化声明的一项受控构建补丁。"""

    name: str
    target_submodule: str
    apply_path: str
    content: bytes

    @property
    def content_hash(self) -> str:
        """返回补丁原始字节的 SHA-256。"""
        return hashlib.sha256(self.content).hexdigest()


class DataInfraAdapter:
    """通过注入式 collector 和 loader 提供 DataInfra 项目事实。"""

    def __init__(
        self,
        root: Path,
        facts_collector: Callable[..., object],
        patch_loader: Callable[[str], Tuple[ManagedPatch, ...]],
    ):
        self.root = root.resolve(strict=False)
        self._facts_collector = facts_collector
        self._patch_loader = patch_loader

    def collect_plan_facts(self, git, *, fresh: bool):
        """收集一次完整计划事实；fresh=True 时 collector 必须先 fetch。"""
        return self._facts_collector(git, fresh=fresh)

    def managed_patches(self, parent_commit: str) -> tuple[ManagedPatch, ...]:
        """返回指定父仓提交声明的不可变受控补丁序列。"""
        return tuple(self._patch_loader(parent_commit))

    def reverse_patch(self, git, patch: ManagedPatch) -> None:
        """从声明的 submodule 与适用路径精确反向应用补丁。"""
        self._apply_patch(git, patch, reverse=True)

    def apply_patch(self, git, patch: ManagedPatch) -> None:
        """向声明的 submodule 与适用路径精确应用补丁。"""
        self._apply_patch(git, patch, reverse=False)

    def patch_state(self, git, patch: ManagedPatch) -> str:
        """返回实际工作树中补丁的 applied、absent 或 invalid 状态。"""
        repository = _safe_directory(self.root, patch.target_submodule)
        if repository is None:
            return "invalid"
        return self._patch_state_at(git, repository, patch)

    def preflight_managed_patches(
        self, git, facts, current_patches, target_patches
    ) -> bool:
        """无领域写入地验证当前 dirty 可清除且目标 pin 可接纳整组补丁。"""
        target_pins = {item.path: item.pin for item in facts.target_submodules}
        patches_by_repository = {}
        for patch in target_patches:
            if (
                patch.target_submodule not in target_pins
                or not _safe_relative_path(patch.target_submodule)
                or not _safe_relative_path(patch.apply_path)
            ):
                return False
            patches_by_repository.setdefault(patch.target_submodule, []).append(patch)

        current_by_repository = {}
        for patch in current_patches:
            current_by_repository.setdefault(patch.target_submodule, []).append(patch)

        for path, patches in patches_by_repository.items():
            current = tuple(current_by_repository.get(path, ()))
            if not self._current_worktree_is_exact(git, path, current):
                return False
            if not self._target_accepts_patches(
                git, path, target_pins[path], tuple(patches)
            ):
                return False
        return set(current_by_repository) == set(patches_by_repository)

    def _apply_patch(self, git, patch: ManagedPatch, *, reverse: bool) -> None:
        """通过临时补丁文件调用 Git argv 边界。"""
        repository = _safe_directory(self.root, patch.target_submodule)
        if repository is None:
            raise GitError(("git", "apply"), "unsafe managed patch target", 2)
        self._apply_patch_at(git, repository, patch, reverse=reverse)

    def _current_worktree_is_exact(self, git, path, patches) -> bool:
        """在工作树副本中移除已应用补丁，确认没有其他 Git 改动。"""
        repository = _safe_directory(self.root, path)
        if repository is None:
            return False
        if any(
            _safe_directory(repository, patch.apply_path) is None
            for patch in patches
        ):
            return False
        git_dir = git.run(
            repository, ("rev-parse", "--absolute-git-dir")
        ).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="data-infra-sync-current-") as directory:
            worktree = Path(directory) / "worktree"
            shutil.copytree(
                repository,
                worktree,
                ignore=shutil.ignore_patterns(".git"),
            )
            git_options = (
                "--git-dir={}".format(git_dir),
                "--work-tree={}".format(worktree),
            )
            for patch in reversed(patches):
                if _safe_directory(worktree, patch.apply_path) is None:
                    return False
                state = self._patch_state_at(
                    git, worktree, patch, git_options=git_options
                )
                if state == "applied":
                    self._apply_patch_at(
                        git,
                        worktree,
                        patch,
                        reverse=True,
                        git_options=git_options,
                    )
                elif state != "absent":
                    return False
            status = git.run(
                worktree,
                git_options
                + ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ).stdout
            return status == ""

    def _target_accepts_patches(self, git, path, target_pin, patches) -> bool:
        """在隔离临时 worktree 中验证目标 pin 可应用或已等价。"""
        repository = _safe_directory(self.root, path)
        if repository is None:
            return False
        with tempfile.TemporaryDirectory(prefix="data-infra-sync-target-") as directory:
            worktree = Path(directory) / "target"
            git.run(
                repository,
                ("worktree", "add", "--detach", str(worktree), target_pin),
            )
            try:
                for patch in patches:
                    if _safe_directory(worktree, patch.apply_path) is None:
                        return False
                    state = self._patch_state_at(git, worktree, patch)
                    if state == "absent":
                        self._apply_patch_at(git, worktree, patch, reverse=False)
                    elif state != "applied":
                        return False
                return True
            finally:
                git.run(
                    repository,
                    ("worktree", "remove", "--force", str(worktree)),
                )

    def _patch_state_at(
        self, git, repository, patch, *, git_options=()
    ) -> str:
        """在指定工作树执行双向 check，区分 applied、absent 与 invalid。"""
        apply_path = _safe_directory(repository, patch.apply_path)
        if apply_path is None:
            return "invalid"
        with tempfile.NamedTemporaryFile(prefix="data-infra-sync-", suffix=".patch") as handle:
            handle.write(patch.content)
            handle.flush()
            reverse = git.run(
                apply_path,
                tuple(git_options)
                + ("apply", "--reverse", "--check", handle.name),
                check=False,
            )
            forward = git.run(
                apply_path,
                tuple(git_options) + ("apply", "--check", handle.name),
                check=False,
            )
        if reverse.returncode == 0 and forward.returncode != 0:
            return "applied"
        if forward.returncode == 0 and reverse.returncode != 0:
            return "absent"
        return "invalid"

    def _apply_patch_at(
        self, git, repository, patch, *, reverse, git_options=()
    ) -> None:
        """在指定工作树通过临时文件应用或反向应用补丁。"""
        apply_path = _safe_directory(repository, patch.apply_path)
        if apply_path is None:
            raise GitError(("git", "apply"), "unsafe managed patch path", 2)
        with tempfile.NamedTemporaryFile(prefix="data-infra-sync-", suffix=".patch") as handle:
            handle.write(patch.content)
            handle.flush()
            args = tuple(git_options) + ("apply",)
            if reverse:
                args += ("--reverse",)
            args += (handle.name,)
            git.run(apply_path, args)


def _safe_relative_path(value):
    """确认 apply path 保持在声明的 submodule 内。"""
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _safe_directory(root, relative):
    """解析并验证 root 内不经过 symlink 的现有目录。"""
    if not _safe_relative_path(relative):
        return None
    try:
        root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    candidate = root
    for part in Path(relative).parts:
        candidate = candidate / part
        try:
            if candidate.is_symlink() or not candidate.exists():
                return None
        except OSError:
            return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != candidate or not resolved.is_dir():
        return None
    return resolved

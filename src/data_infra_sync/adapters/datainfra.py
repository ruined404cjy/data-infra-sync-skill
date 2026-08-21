"""DataInfra 项目的同步事实与受控补丁声明边界。"""

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Tuple


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

    def _apply_patch(self, git, patch: ManagedPatch, *, reverse: bool) -> None:
        """通过临时补丁文件调用 Git argv 边界。"""
        repository = self.root / patch.target_submodule / patch.apply_path
        with tempfile.NamedTemporaryFile(prefix="data-infra-sync-", suffix=".patch") as handle:
            handle.write(patch.content)
            handle.flush()
            args = ("apply", "--reverse", handle.name) if reverse else ("apply", handle.name)
            git.run(repository, args)

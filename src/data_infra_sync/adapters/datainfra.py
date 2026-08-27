"""DataInfra 项目的同步事实与受控补丁声明边界。"""

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from data_infra_sync.git import GitError, RepoFacts, git_environment
from data_infra_sync.planner import PlanFacts, RepositoryPlanFacts, SubmoduleSpec
from data_infra_sync.state import (
    MANAGED_PATCH_RECOVERY_FORMAT,
    MANAGED_PATCH_RECOVERY_STAGES,
    ManagedPatchRecoveryCleanupError,
    ManagedPatchRecoveryValidationError,
    StateStore,
    managed_patch_recovery_identity,
    valid_managed_patch_recovery_document,
)


_ARTIFACT_GROUPS = (
    ("bridge", (
        "deps/iceberg-rust-bridge/target/release/libiceberg_rust_bridge.so",
        "plugins/openGauss-Catalog/deps/libiceberg_rust_bridge.so",
        "mppdb_temp_install/lib/postgresql/libiceberg_rust_bridge.so",
    )),
    ("catalog", (
        "plugins/openGauss-Catalog/iceberg_catalog.so",
        "mppdb_temp_install/lib/postgresql/iceberg_catalog.so",
        "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_catalog.so",
    )),
    ("fdw", (
        "plugins/iceberg_fdw/iceberg_fdw.so",
        "mppdb_temp_install/lib/postgresql/iceberg_fdw.so",
        "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_fdw.so",
    )),
    ("delta", (
        "plugins/iceberg_delta/tmp_build_gcc10/iceberg_delta.so",
        "mppdb_temp_install/lib/postgresql/iceberg_delta.so",
        "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_delta.so",
    )),
    ("catalog-control", (
        "plugins/openGauss-Catalog/iceberg_catalog.control",
        "mppdb_temp_install/share/postgresql/extension/iceberg_catalog.control",
    )),
    ("catalog-sql", (
        "plugins/openGauss-Catalog/iceberg_catalog--1.0.0.sql",
        "mppdb_temp_install/share/postgresql/extension/iceberg_catalog--1.0.0.sql",
    )),
    ("fdw-control", (
        "plugins/iceberg_fdw/iceberg_fdw.control",
        "mppdb_temp_install/share/postgresql/extension/iceberg_fdw.control",
    )),
    ("fdw-sql", (
        "plugins/iceberg_fdw/iceberg_fdw--0.1.0.sql",
        "mppdb_temp_install/share/postgresql/extension/iceberg_fdw--0.1.0.sql",
    )),
    ("delta-control", (
        "plugins/iceberg_delta/iceberg_delta.control",
        "mppdb_temp_install/share/postgresql/extension/iceberg_delta.control",
    )),
    ("delta-sql", (
        "plugins/iceberg_delta/iceberg_delta--1.0.0.sql",
        "mppdb_temp_install/share/postgresql/extension/iceberg_delta--1.0.0.sql",
    )),
)

_CRITICAL_INSTALL_PATHS = (
    ("libiceberg_rust_bridge.so", (
        "mppdb_temp_install/lib/postgresql/libiceberg_rust_bridge.so",
    )),
    ("iceberg_catalog.so", (
        "mppdb_temp_install/lib/postgresql/iceberg_catalog.so",
        "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_catalog.so",
    )),
    ("iceberg_fdw.so", (
        "mppdb_temp_install/lib/postgresql/iceberg_fdw.so",
        "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_fdw.so",
    )),
    ("iceberg_delta.so", (
        "mppdb_temp_install/lib/postgresql/iceberg_delta.so",
        "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_delta.so",
    )),
)

_DELTA_PATCH_PATH = "build/patches/iceberg-delta-cmake-pie-filter.patch"
_DELTA_SUBMODULE = "plugins/iceberg_delta"


class DataInfraInstallAdapter:
    """声明 DataInfra 原生安装布局和可注入进程读取边界。"""

    def __init__(
        self, root: Path, *, file_reader=None, git_reader=None, proc_reader=None
    ):
        self.root = Path(root).resolve(strict=False)
        self.file_reader = file_reader
        self.git_reader = git_reader or self._read_git
        self.proc_reader = proc_reader or self._read_proc

    def artifact_groups(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """返回以 workspace root 为基准的产物一致性组。"""
        return _ARTIFACT_GROUPS

    def critical_install_paths(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """返回运行中关键库允许映射的安装副本相对路径。"""
        return _CRITICAL_INSTALL_PATHS

    def repository_heads(self) -> tuple[tuple[str, str], ...]:
        """读取父仓和 index 声明的全部一级 submodule 实际 HEAD。"""
        parent = self._git_head(self.root)
        listing = self.git_reader(self.root, ("ls-files", "-s", "-z"))
        if isinstance(listing, str):
            listing = listing.encode("utf-8")
        repositories = [(".", parent)]
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode = metadata.split(b" ", 1)[0]
            if mode != b"160000":
                continue
            relative = raw_path.decode("utf-8", errors="strict")
            repository = _safe_directory(self.root, relative)
            if repository is None:
                raise OSError("unsafe or missing submodule: " + relative)
            repositories.append((relative, self._git_head(repository)))
        return tuple(sorted(repositories))

    def process_records(self):
        """返回进程读取器产生的快照，供核验层应用映射规则。"""
        return tuple(self.proc_reader())

    def _git_head(self, repository: Path) -> str:
        """读取指定仓库当前实际 HEAD。"""
        output = self.git_reader(repository, ("rev-parse", "HEAD"))
        if isinstance(output, bytes):
            output = output.decode("ascii")
        return output.strip()

    @staticmethod
    def _read_git(repository: Path, args) -> bytes:
        """通过无 shell argv subprocess 执行只读 Git 命令。"""
        return subprocess.run(
            ("git", "-C", str(repository)) + tuple(args),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment(),
        ).stdout

    @staticmethod
    def _read_proc():
        """仅为名称精确等于 gaussdb 的进程读取 exe 与 maps。"""
        records = []
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                name = _read_proc_text(process / "comm").strip()
                if name != "gaussdb":
                    continue
                exe = os.readlink(process / "exe")
                maps = _read_proc_text(process / "maps").splitlines()
            except OSError:
                continue
            records.append({"name": name, "exe": exe, "maps": tuple(maps)})
        return tuple(records)


def _read_proc_text(path: Path) -> str:
    """以替换字符容忍 Linux procfs 中的非 UTF-8 瞬时字节。"""
    return path.read_text(encoding="utf-8", errors="replace")


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
        recovery_store=None,
        workspace_identity=None,
    ):
        self.root = root.resolve(strict=False)
        self._facts_collector = facts_collector
        self._patch_loader = patch_loader
        self._recovery_store = recovery_store
        self._workspace_identity = workspace_identity
        self._active_recovery = None

    @classmethod
    def for_workspace(cls, config, git, process_reader=None):
        """为现有父仓构造生产事实采集器与版本化补丁读取器。"""
        root = Path(config.root).resolve(strict=False)
        reader = process_reader or DataInfraInstallAdapter._read_proc
        adapter = None

        def patch_loader(parent_commit):
            return _load_managed_patches(git, root, parent_commit)

        def facts_collector(runtime_git, *, fresh):
            return _collect_workspace_facts(
                adapter, config, runtime_git, reader, fresh=fresh
            )

        workspace_identity = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
        adapter = cls(
            root,
            facts_collector,
            patch_loader,
            StateStore(Path(config.state_dir)),
            workspace_identity,
        )
        return adapter

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

    def managed_patch_states(self, git, patches, target_pins=None):
        """按仓库有序组返回每项补丁的逻辑 applied 或 absent 状态。"""
        states = [None] * len(patches)
        by_repository = {}
        for index, patch in enumerate(patches):
            by_repository.setdefault(patch.target_submodule, []).append(
                (index, patch)
            )
        for path, indexed in by_repository.items():
            repository = _safe_directory(self.root, path)
            if repository is None:
                for index, _ in indexed:
                    states[index] = "invalid"
                continue
            group = tuple(patch for _, patch in indexed)
            baseline = 0
            target_pin = None if target_pins is None else target_pins.get(path)
            if target_pin is not None:
                head = git.run(repository, ("rev-parse", "HEAD")).stdout.strip()
                if head == target_pin:
                    baseline = self._target_patch_progress(
                        git, path, target_pin, group
                    )
                    if baseline is None:
                        for index, _ in indexed:
                            states[index] = "invalid"
                        continue
            progress = self._current_patch_progress(
                git, path, group, baseline=baseline
            )
            for group_index, (index, _) in enumerate(indexed):
                states[index] = (
                    "invalid"
                    if progress is None
                    else "applied" if group_index < progress else "absent"
                )
        return tuple(states)

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

    def begin_managed_patch_recovery(
        self, facts, current_patches, target_patches
    ):
        """首次 reverse 前创建或复用已验证的原子恢复日志。"""
        if self._recovery_store is None or not target_patches:
            return None
        if self._active_recovery is not None:
            return False
        document = {
            "format": MANAGED_PATCH_RECOVERY_FORMAT,
            "workspace": self._workspace_identity,
            "target_remote": managed_patch_recovery_identity(facts.target_remote),
            "target_branch": managed_patch_recovery_identity(facts.target_branch),
            "source_parent": facts.current_parent,
            "target_parent": facts.target_parent,
            "target_gitlinks": {
                item.path: item.pin
                for item in sorted(facts.target_submodules, key=lambda item: item.path)
            },
            "patches": [
                _managed_patch_document(item) for item in target_patches
            ],
            "stage": "reversing",
        }
        self._recovery_store.write_managed_patch_recovery(document)
        self._active_recovery = document
        return True

    def advance_managed_patch_recovery(self, stage) -> None:
        """单调推进恢复阶段；较早阶段的重入请求保持当前记录。"""
        if self._active_recovery is None:
            return
        current = self._active_recovery["stage"]
        if MANAGED_PATCH_RECOVERY_STAGES.index(
            stage
        ) <= MANAGED_PATCH_RECOVERY_STAGES.index(current):
            return
        document = dict(self._active_recovery)
        document["stage"] = stage
        self._recovery_store.write_managed_patch_recovery(document)
        self._active_recovery = document

    def clear_managed_patch_recovery(self) -> None:
        """删除当前 workspace 的受控补丁恢复资格。"""
        if self._recovery_store is None:
            return
        self._recovery_store.clear_managed_patch_recovery()
        self._active_recovery = None

    def has_managed_patch_recovery(self) -> bool:
        """返回本次采集是否验证了一份持久化恢复日志。"""
        return self._active_recovery is not None

    def target_contains_managed_patches(self, git, facts, patches) -> bool:
        """确认当前 exact target 的 clean 内容已经等价包含整组补丁。"""
        target_pins = {item.path: item.pin for item in facts.target_submodules}
        repositories = {item.path: item for item in facts.repositories}
        by_repository = {}
        for patch in patches:
            by_repository.setdefault(patch.target_submodule, []).append(patch)
        if not by_repository:
            return False
        for path, items in by_repository.items():
            repository = repositories.get(path)
            if (
                repository is None
                or repository.facts.head != target_pins.get(path)
                or repository.facts.worktree != "clean"
                or self._target_patch_progress(
                    git, path, target_pins[path], tuple(items)
                ) != len(items)
            ):
                return False
        return True

    def _managed_patch_recovery_matches(
        self, git, facts, current_patches, target_patches
    ) -> bool:
        """严格验证恢复日志的声明绑定与阶段允许的实际 Git 状态。"""
        if self._recovery_store is None:
            return False
        try:
            document = self._recovery_store.read_managed_patch_recovery()
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            self._discard_invalid_recovery(facts, None)
            return False
        if document is None:
            self._active_recovery = None
            return False
        if not _valid_recovery_document_shape(document):
            self._discard_invalid_recovery(facts, document)
            return False
        try:
            valid = self._valid_managed_patch_recovery(
                git, facts, current_patches, target_patches, document
            )
        except (GitError, OSError, RuntimeError, ValueError, TypeError) as error:
            raise ManagedPatchRecoveryValidationError(facts) from error
        if not valid:
            self._discard_invalid_recovery(facts, document)
            return False
        self._active_recovery = document
        return True

    def _discard_invalid_recovery(self, facts, document) -> None:
        """清理失配日志；清理失败时保留可能已写入的事实边界。"""
        try:
            self.clear_managed_patch_recovery()
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            raise ManagedPatchRecoveryCleanupError(
                facts, _valid_recovery_document_shape(document)
            ) from error

    def _apply_patch(self, git, patch: ManagedPatch, *, reverse: bool) -> None:
        """通过临时补丁文件调用 Git argv 边界。"""
        repository = _safe_directory(self.root, patch.target_submodule)
        if repository is None:
            raise GitError(("git", "apply"), "unsafe managed patch target", 2)
        self._apply_patch_at(git, repository, patch, reverse=reverse)

    def _current_worktree_is_exact(self, git, path, patches) -> bool:
        """确认工作树是声明顺序的一个精确补丁前缀且没有其他改动。"""
        return self._current_patch_progress(git, path, patches) is not None

    def _current_patch_progress(self, git, path, patches, *, baseline=0):
        """返回基于提交内置前缀的工作树有序补丁进度。"""
        repository = _safe_directory(self.root, path)
        if repository is None or not 0 <= baseline <= len(patches):
            return None
        if any(
            _safe_directory(repository, patch.apply_path) is None
            for patch in patches
        ):
            return None
        git_dir = git.run(
            repository, ("rev-parse", "--absolute-git-dir")
        ).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="data-infra-sync-current-") as directory:
            matches = []
            for progress in range(baseline, len(patches) + 1):
                name = "worktree" if progress == 0 else "worktree-{}".format(progress)
                worktree = Path(directory) / name
                shutil.copytree(
                    repository,
                    worktree,
                    symlinks=True,
                    ignore=shutil.ignore_patterns(".git"),
                )
                git_options = (
                    "--git-dir={}".format(git_dir),
                    "--work-tree={}".format(worktree),
                )
                try:
                    for patch in reversed(patches[baseline:progress]):
                        self._apply_patch_at(
                            git,
                            worktree,
                            patch,
                            reverse=True,
                            git_options=git_options,
                        )
                except GitError:
                    continue
                status = git.run(
                    worktree,
                    git_options
                    + ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                ).stdout
                if status == "":
                    matches.append(progress)
            if len(matches) == 1:
                return matches[0]
            return None

    def _valid_managed_patch_recovery(
        self, git, facts, current_patches, target_patches, document
    ) -> bool:
        """验证日志字段、提交绑定、声明顺序及阶段对应的实际补丁前缀。"""
        if not _valid_recovery_document_shape(document):
            return False
        target_gitlinks = {
            item.path: item.pin
            for item in sorted(facts.target_submodules, key=lambda item: item.path)
        }
        expected_patches = [_managed_patch_document(item) for item in target_patches]
        if (
            document["workspace"] != self._workspace_identity
            or document["target_remote"]
            != managed_patch_recovery_identity(facts.target_remote)
            or document["target_branch"]
            != managed_patch_recovery_identity(facts.target_branch)
            or document["target_parent"] != facts.target_parent
            or document["target_gitlinks"] != target_gitlinks
            or document["patches"] != expected_patches
            or facts.current_parent
            not in (document["source_parent"], document["target_parent"])
        ):
            return False
        source_patches = self.managed_patches(document["source_parent"])
        if (
            [_managed_patch_document(item) for item in source_patches]
            != expected_patches
            or tuple(_managed_patch_key(item) for item in current_patches)
            != tuple(_managed_patch_key(item) for item in target_patches)
            or git.relation(
                self.root, document["source_parent"], document["target_parent"]
            )
            not in ("equal", "contained")
        ):
            return False

        source_gitlinks = {
            item.path: item.pin
            for item in _submodules_at(git, self.root, document["source_parent"])
        }
        repositories = {item.path: item for item in facts.repositories}
        by_repository = {}
        for patch in target_patches:
            by_repository.setdefault(patch.target_submodule, []).append(patch)
        if set(by_repository).difference(source_gitlinks) or set(
            by_repository
        ).difference(target_gitlinks):
            return False

        stage = document["stage"]
        parent_is_source = facts.current_parent == document["source_parent"]
        parent_is_target = facts.current_parent == document["target_parent"]
        if stage == "reversing" and not parent_is_source:
            return False
        if stage in ("submodule_update", "replay", "postcondition") and not parent_is_target:
            return False

        for path, repository in repositories.items():
            head = repository.facts.head
            source_pin = source_gitlinks.get(path)
            target_pin = target_gitlinks.get(path)
            if stage == "reversing" and head != source_pin:
                return False
            if stage == "parent_update" and head != source_pin:
                return False
            if stage == "submodule_update" and head not in (
                source_pin,
                target_pin,
            ):
                return False
            if stage in ("replay", "postcondition") and head != target_pin:
                return False

        for path, patches in by_repository.items():
            repository = repositories.get(path)
            if repository is None or repository.facts.operation is not None:
                return False
            target_pin = target_gitlinks[path]
            baseline = 0
            if repository.facts.head == target_pin:
                baseline = self._target_patch_progress(
                    git, path, target_pin, tuple(patches)
                )
                if baseline is None:
                    return False
            progress = self._current_patch_progress(
                git, path, tuple(patches), baseline=baseline
            )
            if progress is None:
                return False
            if stage == "parent_update" and progress != 0:
                return False
            if stage == "submodule_update" and progress != baseline:
                return False
            if stage == "postcondition" and progress != len(patches):
                return False
        return True

    def _target_accepts_patches(self, git, path, target_pin, patches) -> bool:
        """确认目标提交内置了声明补丁的唯一有序前缀。"""
        return self._target_patch_progress(git, path, target_pin, patches) is not None

    def _target_patch_progress(self, git, path, target_pin, patches):
        """返回目标提交已内置的唯一有序补丁前缀长度。"""
        repository = _safe_directory(self.root, path)
        if repository is None:
            return None
        with tempfile.TemporaryDirectory(prefix="data-infra-sync-target-") as directory:
            matches = []
            for progress in range(len(patches) + 1):
                worktree = Path(directory) / "target-{}".format(progress)
                worktree.mkdir()
                git.run(worktree, ("init", "--quiet"))
                git.run(
                    worktree,
                    (
                        "fetch",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "--refmap=",
                        "--",
                        str(repository),
                        target_pin,
                    ),
                )
                git.run(worktree, ("checkout", "--detach", "FETCH_HEAD"))
                try:
                    for patch in reversed(patches[:progress]):
                        self._apply_patch_at(git, worktree, patch, reverse=True)
                    for patch in patches:
                        self._apply_patch_at(git, worktree, patch, reverse=False)
                except GitError:
                    continue
                matches.append(progress)
            if len(matches) == 1:
                return matches[0]
            return None

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


def _collect_workspace_facts(adapter, config, git, process_reader, *, fresh):
    """读取父仓、一级子仓、进程与补丁连续性的完整计划事实。"""
    root = adapter.root
    target_ref = _target_ref(config.target_remote, config.target_branch)
    _validate_target_ref(git, root, target_ref, config.target_remote)
    if fresh:
        _fetch_parent_target(
            git, root, config.target_remote, config.target_branch, target_ref
        )
    current_parent = git.run(root, ("rev-parse", "HEAD")).stdout.strip()
    if not current_parent:
        raise GitError(("git", "rev-parse", "HEAD"), "parent HEAD is missing", 2)
    current_submodules = _submodules_at(git, root, current_parent)
    if fresh:
        for spec in current_submodules:
            repository = _existing_repository(git, root, spec.path)
            if repository is not None:
                _fetch_submodule_upstream(git, repository)

    target_parent = _resolve_target(git, root, target_ref)
    target_submodules = (
        () if target_parent is None else _submodules_at(git, root, target_parent)
    )
    prefetched_nested = False
    if fresh and target_submodules:
        prefetched_nested = _prefetch_target_objects(
            git, root, config.target_remote, current_submodules, target_submodules
        )
    parent = git.inspect_repo(root)
    if parent.head != current_parent:
        raise GitError(("git", "rev-parse", "HEAD"), "parent HEAD changed during collection", 2)
    parent_relation = (
        "not_applicable"
        if target_parent is None
        else git.relation(root, current_parent, target_parent)
    )

    current_by_path = {item.path: item for item in current_submodules}
    target_by_path = {item.path: item for item in target_submodules}
    repositories = []
    nested_submodules = prefetched_nested
    for logical_path in sorted(set(current_by_path) | set(target_by_path)):
        existing = _existing_repository(git, root, logical_path)
        repository_path = existing or (Path(root) / logical_path)
        observed = (
            git.inspect_repo(repository_path)
            if existing is not None
            else _missing_repo_facts(repository_path)
        )
        current_pin = (
            current_by_path[logical_path].pin
            if logical_path in current_by_path
            else None
        )
        target_pin = (
            target_by_path[logical_path].pin
            if logical_path in target_by_path
            else None
        )
        if observed.head is not None:
            for pin in (current_pin, target_pin):
                if pin is not None:
                    _require_commit(git, repository_path, pin)
            relation = (
                "not_applicable"
                if target_pin is None
                else git.relation(repository_path, observed.head, target_pin)
            )
            nested_submodules = nested_submodules or _has_nested_submodule(
                git, repository_path, observed.head
            )
            if target_pin is not None:
                nested_submodules = nested_submodules or bool(
                    git.gitlinks(repository_path, target_pin)
                )
        else:
            relation = "not_applicable"
        repositories.append(
            RepositoryPlanFacts(
                logical_path,
                observed,
                current_pin,
                target_pin,
                relation,
                "none",
            )
        )

    facts = PlanFacts(
        parent,
        current_parent,
        target_parent,
        config.target_remote,
        config.target_branch,
        config.target_branch,
        parent_relation,
        _parent_non_submodule_dirty(git, root, current_submodules),
        tuple(current_submodules),
        tuple(target_submodules),
        tuple(repositories),
        _workspace_has_running_gaussdb(root, process_reader()),
        nested_submodules,
        stale_target=not fresh,
    )
    return _with_managed_patch_states(adapter, git, facts)


def _target_ref(remote, branch):
    """构造目标远程跟踪引用并拒绝可被解析为选项的 remote。"""
    if (
        not remote
        or not branch
        or remote.startswith("-")
        or any(character in remote + branch for character in "\r\n\0")
    ):
        raise GitError(("git", "check-ref-format"), "invalid target ref", 2)
    return "refs/remotes/{}/{}".format(remote, branch)


def _validate_target_ref(git, root, target_ref, remote):
    """要求目标使用 Git 接受的完整远程跟踪引用。"""
    completed = git.run(root, ("check-ref-format", target_ref), check=False)
    if completed.returncode != 0:
        raise GitError(
            tuple(str(item) for item in completed.args),
            completed.stderr or "invalid target ref",
            completed.returncode,
        )
    remote_exists = git.run(
        root, ("config", "--get", "remote.{}.url".format(remote)), check=False
    )
    if remote_exists.returncode != 0:
        raise GitError(
            tuple(str(item) for item in remote_exists.args),
            remote_exists.stderr or "target remote is missing",
            remote_exists.returncode,
        )


def _fetch_parent_target(git, root, remote, branch, target_ref):
    """仅将目标父仓分支更新到指定远程跟踪引用。"""
    source = "refs/heads/{}".format(branch)
    _fetch_tracking_ref(git, root, remote, source, target_ref)


def _fetch_submodule_upstream(git, repository):
    """仅更新当前子仓分支的安全远程跟踪 upstream。"""
    branch = git.run(
        repository, ("symbolic-ref", "--quiet", "--short", "HEAD"), check=False
    )
    if branch.returncode == 1:
        return
    if branch.returncode != 0:
        raise GitError(
            tuple(str(item) for item in branch.args),
            branch.stderr,
            branch.returncode,
        )
    name = branch.stdout.strip()
    remote = git.run(
        repository, ("config", "--get", "branch.{}.remote".format(name)), check=False
    )
    merge = git.run(
        repository, ("config", "--get", "branch.{}.merge".format(name)), check=False
    )
    if remote.returncode == 1 or merge.returncode == 1:
        return
    if remote.returncode != 0 or merge.returncode != 0:
        failed = remote if remote.returncode != 0 else merge
        raise GitError(
            tuple(str(item) for item in failed.args),
            failed.stderr,
            failed.returncode,
        )
    remote_name = remote.stdout.strip()
    source = merge.stdout.strip()
    if remote_name == ".":
        return
    if not source.startswith("refs/heads/"):
        raise GitError(("git", "fetch"), "unsafe submodule upstream", 2)
    local_branch = "refs/heads/{}".format(name)
    _require_valid_ref(git, repository, local_branch)
    upstream = git.run(
        repository,
        ("for-each-ref", "--format=%(upstream)", local_branch),
        check=False,
    )
    if upstream.returncode != 0:
        raise GitError(
            tuple(str(item) for item in upstream.args),
            upstream.stderr or "submodule upstream is missing",
            upstream.returncode,
        )
    destination = upstream.stdout.strip()
    if not destination.startswith("refs/remotes/"):
        raise GitError(("git", "fetch"), "unsafe submodule upstream", 2)
    _fetch_tracking_ref(git, repository, remote_name, source, destination)


def _fetch_tracking_ref(git, repository, remote, source, destination):
    """只抓取 source，再以不解引用方式原子更新安全 tracking ref。"""
    _require_valid_ref(git, repository, source)
    _require_valid_ref(git, repository, destination)
    git.run(
        repository,
        (
            "fetch",
            "--no-recurse-submodules",
            "--refmap=",
            "--",
            remote,
            source,
        ),
    )
    fetched = git.run(
        repository, ("rev-parse", "--verify", "FETCH_HEAD^{commit}")
    )
    commit = fetched.stdout.strip()
    if not commit:
        raise GitError(
            tuple(str(item) for item in fetched.args),
            "empty fetched commit",
            2,
        )
    git.run(repository, ("update-ref", "--no-deref", destination, commit))


def _require_valid_ref(git, repository, ref):
    """要求 ref 通过 Git 完整引用格式校验。"""
    completed = git.run(repository, ("check-ref-format", ref), check=False)
    if completed.returncode != 0:
        raise GitError(
            tuple(str(item) for item in completed.args),
            completed.stderr or "invalid ref",
            completed.returncode,
        )


def _resolve_target(git, root, target_ref):
    """读取完整远程跟踪引用；引用存在时要求其对象为 commit。"""
    exists = git.run(
        root, ("show-ref", "--verify", "--quiet", target_ref), check=False
    )
    if exists.returncode == 1:
        return None
    if exists.returncode != 0:
        raise GitError(
            tuple(str(item) for item in exists.args), exists.stderr, exists.returncode
        )
    completed = git.run(
        root, ("rev-parse", "--verify", "{}^{{commit}}".format(target_ref))
    )
    target = completed.stdout.strip()
    if not target:
        raise GitError(tuple(str(item) for item in completed.args), "empty target", 2)
    return target


def _submodules_at(git, parent, commit):
    """从同一父仓提交解析完整一级声明及对应 gitlink pin。"""
    links = git.gitlinks(parent, commit)
    listing = git.run(
        parent, ("ls-tree", "-z", commit, "--", ".gitmodules")
    ).stdout
    if not listing:
        if links:
            raise GitError(("git", "ls-tree", commit), "undeclared gitlink", 2)
        return ()
    section_names = _submodule_section_names(git, parent, commit)
    completed = git.run(
        parent,
        ("config", "--null", "--blob", "{}:.gitmodules".format(commit), "--list"),
    )
    declarations = {}
    for entry in completed.stdout.split("\0"):
        if not entry:
            continue
        key, separator, value = entry.partition("\n")
        if not separator or not key.startswith("submodule."):
            raise GitError(tuple(str(item) for item in completed.args), "invalid .gitmodules", 2)
        identity, dot, field = key[len("submodule."):].rpartition(".")
        if not dot or not identity:
            raise GitError(tuple(str(item) for item in completed.args), "invalid .gitmodules", 2)
        if field not in ("path", "url"):
            continue
        fields = declarations.setdefault(identity, {})
        if field in fields:
            raise GitError(
                tuple(str(item) for item in completed.args),
                "duplicate .gitmodules key",
                2,
            )
        fields[field] = value

    if len(section_names) != len(set(section_names)) or set(section_names) != set(
        declarations
    ):
        raise GitError(
            ("git", "show", "{}:.gitmodules".format(commit)),
            "ambiguous submodule section",
            2,
        )

    specs = []
    seen_paths = set()
    for name, fields in declarations.items():
        if (
            set(fields) != {"path", "url"}
            or not fields["url"]
            or not _safe_submodule_path(fields["path"])
        ):
            raise GitError(("git", "config", "--blob"), "unsafe submodule declaration", 2)
        path = fields["path"]
        if path in seen_paths or path not in links:
            raise GitError(("git", "ls-tree", commit), "ambiguous submodule declaration", 2)
        seen_paths.add(path)
        specs.append(SubmoduleSpec(name, path, fields["url"], links[path].commit))
    if seen_paths != set(links):
        raise GitError(("git", "ls-tree", commit), "undeclared gitlink", 2)
    return tuple(sorted(specs, key=lambda item: (item.path, item.name, item.url)))


_SUBMODULE_SECTION = re.compile(
    r'^\s*\[\s*submodule\s+"((?:[^"\\]|\\["\\])*)"\s*\]\s*(?:[#;].*)?$',
    re.IGNORECASE,
)


def _submodule_section_names(git, parent, commit):
    """读取原始 blob，并返回每个标准 quoted submodule section 的 name。"""
    completed = git.run(parent, ("show", "{}:.gitmodules".format(commit)))
    names = []
    for line in completed.stdout.splitlines():
        if not line.lstrip().lower().startswith("[submodule"):
            continue
        match = _SUBMODULE_SECTION.fullmatch(line)
        if match is None:
            raise GitError(
                tuple(str(item) for item in completed.args),
                "invalid submodule section",
                2,
            )
        names.append(match.group(1).replace('\\"', '"').replace("\\\\", "\\"))
    return tuple(names)


def _safe_submodule_path(value):
    """确认 submodule 声明是父仓内非空逻辑路径。"""
    path = Path(value)
    return (
        bool(value)
        and value != "."
        and not path.is_absolute()
        and ".." not in path.parts
        and not any(part in ("", ".") for part in path.parts)
    )


def _existing_repository(git, root, logical_path):
    """返回安全的现有 submodule 目录，缺失目录返回 None。"""
    path = Path(root) / logical_path
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    repository = _safe_directory(root, logical_path)
    if repository is None:
        raise GitError(("git", "-C", logical_path), "unsafe submodule path", 2)
    top = git.run(repository, ("rev-parse", "--show-toplevel"), check=False)
    if top.returncode == 0:
        try:
            actual = Path(top.stdout.strip()).resolve(strict=True)
        except (OSError, RuntimeError):
            actual = None
        if actual == repository:
            return repository
    try:
        empty = not any(repository.iterdir())
    except OSError:
        empty = False
    if empty:
        return None
    if top.returncode not in (0, 128):
        raise GitError(tuple(str(item) for item in top.args), top.stderr, top.returncode)
    raise GitError(("git", "rev-parse", "--show-toplevel"), "submodule root mismatch", 2)


def _missing_repo_facts(path):
    """构造不会向上发现父仓的缺失 submodule 事实。"""
    return RepoFacts(
        Path(path).resolve(strict=False),
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


def _require_commit(git, repository, commit):
    """目标 pin 在现有子仓对象库中必须可读为 commit。"""
    completed = git.run(
        repository, ("cat-file", "-e", "{}^{{commit}}".format(commit)), check=False
    )
    if completed.returncode != 0:
        raise GitError(
            tuple(str(item) for item in completed.args),
            completed.stderr or "submodule target object is missing",
            completed.returncode,
        )


def _prefetch_target_objects(
    git, root, parent_remote, current_submodules, target_submodules
):
    """fresh 模式按目标 URL 预取并验证全部目标 pin。"""
    current_paths = {item.path for item in current_submodules}
    parent_url = git.run(
        root, ("config", "--get", "remote.{}.url".format(parent_remote))
    ).stdout.strip()
    if not parent_url:
        raise GitError(("git", "config", parent_remote), "empty parent remote URL", 2)
    nested = False
    for target in target_submodules:
        url = _resolve_submodule_url(root, parent_url, target.url)
        repository = _existing_repository(git, root, target.path)
        if repository is not None and target.path in current_paths:
            if not _commit_exists(git, repository, target.pin):
                git.run(
                    repository,
                    (
                        "fetch",
                        "--no-tags",
                        "--no-recurse-submodules",
                        "--refmap=",
                        "--",
                        url,
                        target.pin,
                    ),
                )
            _require_commit(git, repository, target.pin)
            nested = nested or bool(git.gitlinks(repository, target.pin))
            continue
        with tempfile.TemporaryDirectory(
            prefix="data-infra-sync-prefetch-"
        ) as directory:
            bare = Path(directory) / "repository.git"
            git.run(root, ("init", "--bare", str(bare)))
            git.run(
                bare,
                (
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--refmap=",
                    "--",
                    url,
                    target.pin,
                ),
            )
            _require_commit(git, bare, target.pin)
            nested = nested or bool(git.gitlinks(bare, target.pin))
    return nested


def _commit_exists(git, repository, commit):
    """仅区分对象存在性；其他读取错误由后续强校验显式抛出。"""
    completed = git.run(
        repository, ("cat-file", "-e", "{}^{{commit}}".format(commit)), check=False
    )
    if completed.returncode not in (0, 1, 128):
        raise GitError(
            tuple(str(item) for item in completed.args),
            completed.stderr,
            completed.returncode,
        )
    return completed.returncode == 0


def _resolve_submodule_url(root, parent_url, submodule_url):
    """按父仓 remote 语义解析 `./` 与 `../` submodule URL。"""
    if (
        not submodule_url
        or any(character in parent_url + submodule_url for character in "\r\n\0")
    ):
        raise GitError(("git", "fetch"), "invalid submodule URL", 2)
    if not submodule_url.startswith(("./", "../")):
        return submodule_url
    parsed = urlsplit(parent_url)
    if parsed.scheme:
        base = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path.rstrip("/") + "/",
                "",
                "",
            )
        )
        return urljoin(base, submodule_url)
    if ":" in parent_url and not parent_url.startswith("/"):
        prefix, path = parent_url.split(":", 1)
        return "{}:{}".format(
            prefix, posixpath.normpath(posixpath.join(path, submodule_url))
        )
    parent_path = Path(parent_url)
    if not parent_path.is_absolute():
        parent_path = Path(root) / parent_path
    return str((parent_path / submodule_url).resolve(strict=False))


def _has_nested_submodule(git, repository, head):
    """识别当前工作区声明或当前提交包含的嵌套 submodule。"""
    if (Path(repository) / ".gitmodules").exists():
        return True
    return bool(git.gitlinks(repository, head))


def _parent_non_submodule_dirty(git, root, current_submodules):
    """忽略纯 gitlink porcelain 项，保留普通 index/worktree/untracked 改动。"""
    submodule_paths = {item.path for item in current_submodules}
    output = git.run(
        root, ("status", "--porcelain=v1", "-z", "--untracked-files=all")
    ).stdout
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4 or entry[2] != " ":
            continue
        path = entry[3:]
        second_path = None
        if entry[0] in ("R", "C") and index < len(entries):
            second_path = entries[index]
            index += 1
        pure_worktree_gitlink = (
            path in submodule_paths
            and second_path is None
            and entry[0:2] == " M"
        )
        if not pure_worktree_gitlink:
            return True
    return False


def _workspace_has_running_gaussdb(root, records):
    """判断进程可执行文件或映射是否属于当前 workspace。"""
    root = Path(root)
    for record in records:
        if record.get("name") != "gaussdb":
            continue
        candidates = [str(record.get("exe", ""))]
        for line in record.get("maps", ()):
            fields = str(line).split(maxsplit=5)
            mapped = fields[5] if len(fields) == 6 else str(line)
            if mapped.startswith("/"):
                candidates.append(mapped[:-10] if mapped.endswith(" (deleted)") else mapped)
        for value in candidates:
            try:
                Path(value).relative_to(root)
            except (ValueError, TypeError):
                continue
            return True
    return False


def _load_managed_patches(git, root, parent_commit):
    """从指定父仓提交读取固定 Delta 构建补丁 blob。"""
    listing = git.run(
        root, ("ls-tree", "-z", parent_commit, "--", _DELTA_PATCH_PATH)
    ).stdout
    if not listing:
        return ()
    revision = "{}:{}".format(parent_commit, _DELTA_PATCH_PATH)
    completed = subprocess.run(
        ("git", "cat-file", "blob", revision),
        cwd=str(root),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=git_environment(),
    )
    if completed.returncode != 0:
        raise GitError(
            tuple(str(item) for item in completed.args),
            completed.stderr.decode("utf-8", errors="replace"),
            completed.returncode,
        )
    return (
        ManagedPatch(
            "iceberg-delta-cmake-pie-filter",
            _DELTA_SUBMODULE,
            ".",
            completed.stdout,
        ),
    )


def _with_managed_patch_states(adapter, git, facts):
    """按声明变化和隔离预检结果设置各子仓补丁状态。"""
    current = adapter.managed_patches(facts.current_parent)
    target = (
        ()
        if facts.target_parent is None
        else adapter.managed_patches(facts.target_parent)
    )
    recovery = adapter._managed_patch_recovery_matches(
        git, facts, current, target
    )
    repository_paths = {item.path for item in facts.repositories}
    global_transition = any(
        item.target_submodule not in repository_paths for item in current + target
    )
    unsupported_count = len(current) > 1 or len(target) > 1
    global_transition = global_transition or unsupported_count
    states = {}
    if unsupported_count:
        states.update(
            (path, "transition")
            for path in {item.target_submodule for item in current + target}
        )
    if facts.target_parent is None:
        repositories = tuple(
            RepositoryPlanFacts(
                item.path,
                item.facts,
                item.current_pin,
                item.target_pin,
                item.relation,
                states.get(item.path, "none"),
            )
            for item in facts.repositories
        )
        return _replace_patch_facts(facts, repositories, global_transition)
    current_keys = tuple(_managed_patch_key(item) for item in current)
    target_keys = tuple(_managed_patch_key(item) for item in target)
    if not unsupported_count and current_keys != target_keys:
        states.update(
            (path, "transition")
            for path in {item.target_submodule for item in current + target}
        )
    elif not unsupported_count and current:
        paths = {item.target_submodule for item in current}
        repositories = {item.path: item for item in facts.repositories}
        dirty = paths.issubset(repositories) and all(
            repositories[path].facts.worktree == "dirty" for path in paths
        )
        integrated = (
            facts.current_parent == facts.target_parent
            and adapter.target_contains_managed_patches(git, facts, target)
        )
        state = "transition"
        if (
            (dirty or recovery or integrated)
            and not global_transition
            and adapter.preflight_managed_patches(git, facts, current, target)
        ):
            state = "continuous"
        states.update((path, state) for path in paths)
    repositories = tuple(
        RepositoryPlanFacts(
            item.path,
            item.facts,
            item.current_pin,
            item.target_pin,
            item.relation,
            states.get(item.path, "none"),
        )
        for item in facts.repositories
    )
    return _replace_patch_facts(facts, repositories, global_transition)


def _replace_patch_facts(facts, repositories, global_transition):
    """保留采集事实并替换补丁仓库状态和全局迁移标记。"""
    return PlanFacts(
        facts.parent,
        facts.current_parent,
        facts.target_parent,
        facts.target_remote,
        facts.target_branch,
        facts.required_parent_branch,
        facts.parent_relation,
        facts.parent_non_submodule_dirty,
        facts.current_submodules,
        facts.target_submodules,
        repositories,
        facts.running_instances,
        facts.nested_submodules,
        facts.stale_target,
        global_transition,
    )


def _managed_patch_key(patch):
    """返回补丁声明连续性所需的稳定字段。"""
    return (
        patch.name,
        patch.content_hash,
        patch.target_submodule,
        patch.apply_path,
    )


def _managed_patch_document(patch):
    """构造不含补丁字节与物理路径的恢复声明。"""
    return {
        "name": patch.name,
        "content_hash": patch.content_hash,
        "target_submodule": patch.target_submodule,
        "apply_path": patch.apply_path,
    }


def _valid_recovery_document_shape(document):
    """严格验证恢复日志的版本、字段集合与规范哈希/OID。"""
    return valid_managed_patch_recovery_document(document)


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

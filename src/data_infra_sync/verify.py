"""DataInfra 源码、安装产物与运行映射身份核验。"""

import errno
import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from data_infra_sync.adapters.datainfra import DataInfraInstallAdapter
from data_infra_sync.model import Action, Result


_SHARED_LIBRARY_NAME = re.compile(
    r"^[^/]+\.so(?:\.[0-9]+(?:\.[0-9]+)*)?$"
)
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class InstallIdentity:
    """保存仅含逻辑相对路径的安装 manifest v1 身份。"""

    repositories: tuple[tuple[str, str], ...]
    artifacts: tuple[tuple[str, str], ...]

    def to_manifest(self) -> dict[str, object]:
        """返回不含机器路径和运行时数据的 manifest v1。"""
        return {
            "format": "1",
            "repositories": dict(self.repositories),
            "artifacts": dict(self.artifacts),
        }


class _VerificationError(Exception):
    """携带可稳定映射到 Result 的当前状态失败分类。"""

    def __init__(self, state: str, reasons: Iterable[str]):
        super().__init__(state)
        self.state = state
        self.reasons = tuple(sorted(set(reasons)))


def collect_install_identity(config, adapter) -> InstallIdentity:
    """收集源码 HEAD、相对产物 SHA，并验证当前副本与进程内部一致。"""
    root = Path(config.root).resolve(strict=True)
    adapter_root = Path(adapter.root).resolve(strict=True)
    if root != adapter_root:
        raise _VerificationError("failed", ("workspace_root_mismatch",))

    try:
        repositories = tuple(adapter.repository_heads())
        if any(
            not _safe_logical_path(path)
            or not isinstance(head, str)
            or _OBJECT_ID.fullmatch(head) is None
            for path, head in repositories
        ):
            raise OSError("unsafe repository path")
    except Exception:
        raise _VerificationError("failed", ("git_read_failed",))

    artifacts = []
    mismatch_reasons = []
    for _, paths in adapter.artifact_groups():
        group_hashes = []
        for relative in paths:
            try:
                digest = _artifact_digest(root, relative, adapter)
            except FileNotFoundError:
                mismatch_reasons.append("artifact_missing")
                continue
            except _NotRegularFile:
                mismatch_reasons.append("artifact_not_regular")
                continue
            except (OSError, RuntimeError):
                raise _VerificationError("failed", ("artifact_read_failed",))
            artifacts.append((relative, digest))
            group_hashes.append(digest)
        if len(group_hashes) == len(paths) and len(set(group_hashes)) != 1:
            mismatch_reasons.append("artifact_group_mismatch")

    try:
        mismatch_reasons.extend(
            _process_mismatches(root, adapter.process_records(), adapter)
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _VerificationError("failed", ("proc_read_failed",))
    if mismatch_reasons:
        raise _VerificationError("deployment_mismatch", mismatch_reasons)
    return InstallIdentity(tuple(sorted(repositories)), tuple(sorted(artifacts)))


def verify_install(config, store, *, record: bool) -> Result:
    """比较或原子记录当前 DataInfra 安装身份并返回结构化状态。"""
    try:
        adapter = DataInfraInstallAdapter(Path(config.root))
        identity = collect_install_identity(config, adapter)
        if record:
            store.write_manifest(identity.to_manifest())
            return _result(identity, "deployment_consistent", (), True)

        try:
            manifest = _read_manifest(store.state_dir)
        except (OSError, RuntimeError, ValueError, TypeError):
            raise _VerificationError("failed", ("manifest_read_failed",))
        if manifest is None:
            return _result(identity, "build_required", ("manifest_missing",), False)
        reasons = _manifest_reasons(identity, manifest)
        if "source_identity_changed" in reasons:
            state = "build_required"
        elif reasons:
            state = "deployment_mismatch"
        else:
            state = "deployment_consistent"
        return _result(identity, state, reasons, False)
    except _VerificationError as error:
        return _result(None, error.state, error.reasons, False)
    except (OSError, RuntimeError, ValueError, TypeError):
        return _result(None, "failed", ("verification_read_failed",), False)


class _NotRegularFile(Exception):
    pass


def _artifact_digest(root: Path, relative: str, adapter) -> str:
    """通过注入读取器或安全默认读取器计算一项产物 SHA-256。"""
    reader = getattr(adapter, "file_reader", None)
    if reader is None:
        return _hash_regular_file(root, relative)
    if not _safe_logical_path(relative) or relative == ".":
        raise _NotRegularFile(relative)
    content = reader(root, relative)
    if not isinstance(content, bytes):
        raise TypeError("file reader must return bytes")
    return hashlib.sha256(content).hexdigest()


def _hash_regular_file(root: Path, relative: str) -> str:
    """不跟随 symlink 地读取 root 内普通文件并计算 SHA-256。"""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise _NotRegularFile(relative)
    digest = hashlib.sha256()
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        with ExitStack() as directories:
            directory = os.open(root, directory_flags)
            directories.callback(os.close, directory)
            for part in path.parts[:-1]:
                directory = os.open(part, directory_flags, dir_fd=directory)
                directories.callback(os.close, directory)
            descriptor = os.open(
                path.parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            try:
                source = os.fdopen(descriptor, "rb")
            except BaseException:
                os.close(descriptor)
                raise
            with source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise _NotRegularFile(relative)
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _NotRegularFile(relative) from error
        raise
    return digest.hexdigest()


def _process_mismatches(root: Path, records, adapter) -> tuple[str, ...]:
    """检查 gaussdb executable、deleted `.so` 和关键库 workspace 归属。"""
    reasons = []
    expected_paths = {
        name: frozenset(root / relative for relative in paths)
        for name, paths in adapter.critical_install_paths()
    }
    for record in records:
        if record.get("name") != "gaussdb":
            continue
        exe = str(record["exe"])
        mappings = tuple(
            mapped
            for mapped in (_mapped_path(str(line)) for line in record["maps"])
            if mapped is not None
        )
        critical_mappings = tuple(
            mapped
            for mapped in mappings
            if Path(_without_deleted_suffix(mapped)).name in expected_paths
        )
        if not _is_within(root, exe) and not critical_mappings:
            continue
        if not _is_within(root, exe):
            reasons.append("other_workspace_mapping")
        for mapped in mappings:
            if mapped.endswith(" (deleted)"):
                original = _without_deleted_suffix(mapped)
                if _SHARED_LIBRARY_NAME.fullmatch(Path(original).name):
                    reasons.append("deleted_library_mapping")
                continue
            name = Path(mapped).name
            if name in expected_paths and Path(mapped) not in expected_paths[name]:
                reasons.append("other_workspace_mapping")
    return tuple(reasons)


def _without_deleted_suffix(value: str) -> str:
    """移除 proc maps 的 deleted 标记以便解析真实 basename。"""
    return value[:-10] if value.endswith(" (deleted)") else value


def _mapped_path(line: str) -> Optional[str]:
    """从 `/proc/<pid>/maps` 行或测试用直接路径中取出映射路径。"""
    if line.startswith("/"):
        return line
    fields = line.split(maxsplit=5)
    if len(fields) == 6 and fields[5].startswith("/"):
        return fields[5]
    return None


def _is_within(root: Path, value: str) -> bool:
    """按绝对词法路径判断映射是否属于当前 workspace。"""
    path = Path(value)
    if not path.is_absolute():
        return False
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_manifest(state_dir: Path) -> Optional[Mapping[str, object]]:
    """不跟随 symlink 地读取并校验 manifest v1 基本结构。"""
    path = Path(state_dir) / "manifest.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("manifest is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise OSError("manifest is not a regular file")
        data = json.load(source)
    if (
        not isinstance(data, dict)
        or data.get("format") != "1"
        or not isinstance(data.get("repositories"), dict)
        or not isinstance(data.get("artifacts"), dict)
        or set(data) != {"format", "repositories", "artifacts"}
    ):
        raise ValueError("invalid manifest v1")
    repositories = data["repositories"]
    artifacts = data["artifacts"]
    if (
        any(
            not _safe_logical_path(path)
            or not isinstance(head, str)
            or _OBJECT_ID.fullmatch(head) is None
            for path, head in repositories.items()
        )
        or any(
            path == "."
            or not _safe_logical_path(path)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            for path, digest in artifacts.items()
        )
    ):
        raise ValueError("invalid manifest paths")
    return data


def _safe_logical_path(value: object) -> bool:
    """确认 manifest 与适配器路径保持为 workspace 内逻辑相对路径。"""
    if not isinstance(value, str):
        return False
    path = Path(value)
    return value == "." or (
        bool(path.parts) and not path.is_absolute() and ".." not in path.parts
    )


def _manifest_reasons(
    identity: InstallIdentity, manifest: Mapping[str, object]
) -> tuple[str, ...]:
    """比较当前身份与旧 manifest 并返回稳定排序原因码。"""
    reasons = []
    if dict(identity.repositories) != manifest["repositories"]:
        reasons.append("source_identity_changed")
    if dict(identity.artifacts) != manifest["artifacts"]:
        reasons.append("artifact_manifest_mismatch")
    return tuple(sorted(reasons))


def _result(identity, state: str, reasons, changed: bool) -> Result:
    """构造符合 result-v1 schema 的安装核验结果。"""
    repositories = () if identity is None else tuple(
        {
            "path": path,
            "role": "parent" if path == "." else "submodule",
            "head": head,
            "target_pin": None,
            "branch": None,
            "upstream": None,
            "ahead": None,
            "behind": None,
            "worktree": "clean",
            "relation": "not_applicable",
            "reason_codes": (),
        }
        for path, head in identity.repositories
    )
    actions = ()
    if state in ("build_required", "deployment_mismatch"):
        actions = (
            Action(
                "verify_install_record",
                ("data-infra-sync", "verify", "install", "--record"),
                False,
                False,
                ("build_completed",),
            ),
        )
    return Result(
        "verify install",
        state,
        tuple(sorted(set(reasons))),
        None,
        repositories,
        changed,
        actions,
        None,
        False,
    )

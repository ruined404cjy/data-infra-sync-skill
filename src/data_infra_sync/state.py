"""状态文件、审计事件和进程锁。"""

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Tuple


_URL_USERINFO = re.compile(r"((?:[a-z][a-z0-9+.-]*://))[^/@\s]+@", re.IGNORECASE)
_SENSITIVE_WORDS = (
    r"access_token|private_token|api_key|token|password|passwd|secret|credential|authorization|auth|key"
)
_URL_TOKEN = re.compile(r"([?&](?:" + _SENSITIVE_WORDS + r")=)[^&#\s]*", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(\b(?:" + _SENSITIVE_WORDS + r")\s*[:=]\s*)[^,\s&#]*", re.IGNORECASE
)
_SENSITIVE_KEY = re.compile(_SENSITIVE_WORDS, re.IGNORECASE)
_SENSITIVE_ENV_NAME = re.compile(_SENSITIVE_WORDS, re.IGNORECASE)
_REDACTED = "[REDACTED]"
_MANAGED_PATCH_RECOVERY = "managed-patch-recovery.json"
MANAGED_PATCH_RECOVERY_FORMAT = "managed-patch-recovery-v1"
MANAGED_PATCH_RECOVERY_STAGES = (
    "reversing",
    "parent_update",
    "submodule_update",
    "replay",
    "postcondition",
)
_RECOVERY_FIELDS = frozenset(
    {
        "format",
        "workspace",
        "target_remote",
        "target_branch",
        "source_parent",
        "target_parent",
        "target_gitlinks",
        "patches",
        "stage",
    }
)
_RECOVERY_PATCH_FIELDS = frozenset(
    {"name", "content_hash", "target_submodule", "apply_path"}
)
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_PROTOCOL_FIELDS = frozenset(
    {
        "schema_version",
        "command",
        "state",
        "changed",
        "snapshot",
        "stale_target",
        "reason_codes",
    }
)
_ACTION_PROTOCOL_FIELDS = frozenset(
    {"kind", "mutates_worktree", "requires_confirmation", "preconditions"}
)
_REPOSITORY_PROTOCOL_FIELDS = frozenset(
    {"role", "head", "target_pin", "ahead", "behind", "worktree", "relation", "reason_codes"}
)
_TARGET_PROTOCOL_FIELDS = frozenset({"parent_commit", "gitlinks"})


class ManagedPatchRecoveryCleanupError(RuntimeError):
    """携带恢复日志清理失败时已经完成采集的实际 facts。"""

    def __init__(self, facts, possible_domain_writes):
        super().__init__("managed patch recovery cleanup failed")
        self.facts = facts
        self.possible_domain_writes = possible_domain_writes


class ManagedPatchRecoveryValidationError(RuntimeError):
    """携带有效恢复记录暂时无法核验时已读取的实际 facts。"""

    def __init__(self, facts):
        super().__init__("managed patch recovery validation failed")
        self.facts = facts


class StateStore:
    """在一个工作区状态目录中维护可恢复的审计数据。"""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    def write_latest(self, result: Any) -> None:
        """原子替换最近一次结构化命令结果。"""
        self._write_json("latest.json", result.to_dict())

    def append_event(self, result: Any) -> None:
        """追加一条脱敏后的结构化命令结果事件。"""
        self._ensure_state_dir()
        with (self.state_dir / "events.jsonl").open("a", encoding="utf-8") as output:
            json.dump(_redact(result.to_dict()), output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())

    def write_manifest(self, data: Mapping[str, Any]) -> None:
        """原子替换脱敏后的安装身份 manifest。"""
        self._write_json("manifest.json", data)

    def read_managed_patch_recovery(self):
        """读取独立补丁恢复日志；文件不存在时返回 None。"""
        path = self.state_dir / _MANAGED_PATCH_RECOVERY
        try:
            with path.open("r", encoding="utf-8") as source:
                return json.load(source)
        except FileNotFoundError:
            return None

    def write_managed_patch_recovery(self, data: Mapping[str, Any]) -> None:
        """原子替换受控补丁恢复日志。"""
        document = serialize_managed_patch_recovery_document(data)
        self._write_json_document(
            _MANAGED_PATCH_RECOVERY, document, sync_directory=True
        )

    def clear_managed_patch_recovery(self) -> None:
        """删除受控补丁恢复日志并持久化目录项。"""
        try:
            (self.state_dir / _MANAGED_PATCH_RECOVERY).unlink()
        except FileNotFoundError:
            return
        self._sync_state_dir()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """获取非阻塞的工作区进程锁，冲突时抛出 BlockingIOError。"""
        self._ensure_state_dir()
        lock_path = self.state_dir / "state.lock"
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_json(
        self, name: str, data: Any, *, sync_directory: bool = False
    ) -> None:
        """以临时文件和 replace 原子写入一份脱敏 JSON。"""
        self._write_json_document(
            name, _redact(data), sync_directory=sync_directory
        )

    def _write_json_document(
        self, name: str, document: Any, *, sync_directory: bool = False
    ) -> None:
        """原子写入已经完成协议变换的 JSON 文档。"""
        self._ensure_state_dir()
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_dir,
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                json.dump(document, temporary, ensure_ascii=False, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.state_dir / name)
            temporary_name = None
            if sync_directory:
                self._sync_state_dir()
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def _ensure_state_dir(self) -> None:
        """创建状态目录，使首次命令也可记录状态。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _sync_state_dir(self) -> None:
        """同步状态目录，持久化原子替换或删除的目录项。"""
        descriptor = os.open(str(self.state_dir), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def serialize_managed_patch_recovery_document(data: Mapping[str, Any]) -> dict:
    """验证并复制恢复协议文档，不执行审计文本脱敏。"""
    if not isinstance(data, Mapping) or set(data) != _RECOVERY_FIELDS:
        raise ValueError("invalid managed patch recovery fields")
    scalar_fields = (
        "format",
        "workspace",
        "target_remote",
        "target_branch",
        "source_parent",
        "target_parent",
        "stage",
    )
    if any(not isinstance(data[field], str) for field in scalar_fields):
        raise ValueError("invalid managed patch recovery scalar")
    if (
        data["format"] != MANAGED_PATCH_RECOVERY_FORMAT
        or _SHA256.fullmatch(data["workspace"]) is None
        or _OBJECT_ID.fullmatch(data["source_parent"]) is None
        or _OBJECT_ID.fullmatch(data["target_parent"]) is None
        or data["stage"] not in MANAGED_PATCH_RECOVERY_STAGES
        or _SHA256.fullmatch(data["target_remote"]) is None
        or _SHA256.fullmatch(data["target_branch"]) is None
    ):
        raise ValueError("invalid managed patch recovery identity")

    gitlinks = data["target_gitlinks"]
    if not isinstance(gitlinks, Mapping) or not gitlinks:
        raise ValueError("invalid managed patch recovery gitlinks")
    canonical_gitlinks = {}
    for path, pin in gitlinks.items():
        if (
            not _valid_recovery_path(path, allow_dot=False)
            or not isinstance(pin, str)
            or _OBJECT_ID.fullmatch(pin) is None
        ):
            raise ValueError("invalid managed patch recovery gitlink")
        canonical_gitlinks[path] = pin

    patches = data["patches"]
    if not isinstance(patches, list) or not patches:
        raise ValueError("invalid managed patch recovery patches")
    canonical_patches = []
    names = set()
    for patch in patches:
        if not isinstance(patch, Mapping) or set(patch) != _RECOVERY_PATCH_FIELDS:
            raise ValueError("invalid managed patch recovery patch fields")
        if any(not isinstance(value, str) for value in patch.values()):
            raise ValueError("invalid managed patch recovery patch scalar")
        if (
            not _valid_recovery_identity(patch["name"])
            or patch["name"] in names
            or _SHA256.fullmatch(patch["content_hash"]) is None
            or not _valid_recovery_path(
                patch["target_submodule"], allow_dot=False
            )
            or patch["target_submodule"] not in canonical_gitlinks
            or not _valid_recovery_path(patch["apply_path"], allow_dot=True)
        ):
            raise ValueError("invalid managed patch recovery patch")
        names.add(patch["name"])
        canonical_patches.append(
            {field: patch[field] for field in _RECOVERY_PATCH_FIELDS}
        )

    return {
        "format": data["format"],
        "workspace": data["workspace"],
        "target_remote": data["target_remote"],
        "target_branch": data["target_branch"],
        "source_parent": data["source_parent"],
        "target_parent": data["target_parent"],
        "target_gitlinks": canonical_gitlinks,
        "patches": canonical_patches,
        "stage": data["stage"],
    }


def valid_managed_patch_recovery_document(data: Any) -> bool:
    """返回文档是否完整符合恢复协议。"""
    try:
        serialize_managed_patch_recovery_document(data)
    except (TypeError, ValueError):
        return False
    return True


def managed_patch_recovery_identity(value: str) -> str:
    """返回恢复协议用的规范 UTF-8 identity 摘要。"""
    if not isinstance(value, str) or not _valid_recovery_identity(value):
        raise ValueError("invalid managed patch recovery identity source")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_recovery_identity(value: str) -> bool:
    """拒绝空值和可扩展为额外文本记录的控制字符。"""
    return bool(value) and not any(character in value for character in "\r\n\0")


def _valid_recovery_path(value: Any, *, allow_dot: bool) -> bool:
    """只接受规范 POSIX 逻辑相对路径。"""
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    if value == ".":
        return allow_dot
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and str(path) == value
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _redact(value: Any, path: Tuple[object, ...] = ()) -> Any:
    """按 JSON 路径递归删除凭据并保留协议控制字段。"""
    if isinstance(value, Mapping):
        return {
            key: _REDACTED
            if _SENSITIVE_KEY.search(str(key))
            else _redact(item, path + (str(key),))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, path + (index,)) for index, item in enumerate(value)]
    if isinstance(value, tuple):
        return [_redact(item, path + (index,)) for index, item in enumerate(value)]
    if isinstance(value, str):
        redacted = _URL_USERINFO.sub(r"\1", value)
        redacted = _URL_TOKEN.sub(r"\1" + _REDACTED, redacted)
        redacted = _SENSITIVE_ASSIGNMENT.sub(r"\1" + _REDACTED, redacted)
        if not _is_protocol_path(path):
            environment_values = sorted(_sensitive_environment_values(), key=len, reverse=True)
            for secret in environment_values:
                redacted = redacted.replace(secret, _REDACTED)
        return redacted
    return value


def _is_protocol_path(path: Tuple[object, ...]) -> bool:
    """判断路径是否指向 schema 控制且不接受环境值替换的字段。"""
    if not path:
        return False
    if path[0] in _TOP_LEVEL_PROTOCOL_FIELDS:
        return True
    if len(path) >= 3 and path[0] == "next_actions":
        return path[2] in _ACTION_PROTOCOL_FIELDS
    if len(path) >= 3 and path[0] == "repositories":
        return path[2] in _REPOSITORY_PROTOCOL_FIELDS
    if len(path) >= 2 and path[0] == "target":
        return path[1] in _TARGET_PROTOCOL_FIELDS
    return False


def _sensitive_environment_values() -> set[str]:
    """返回按名称规则判定为敏感的非空环境变量值。"""
    return {
        value
        for name, value in os.environ.items()
        if value
        and (
            name.upper().startswith("DATA_INFRA_SYNC_")
            or _SENSITIVE_ENV_NAME.search(name) is not None
        )
    }

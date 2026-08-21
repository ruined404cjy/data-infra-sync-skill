"""状态文件、审计事件和进程锁。"""

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
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

    def _write_json(self, name: str, data: Any) -> None:
        """以临时文件和 replace 原子写入一份脱敏 JSON。"""
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
                json.dump(_redact(data), temporary, ensure_ascii=False, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.state_dir / name)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def _ensure_state_dir(self) -> None:
        """创建状态目录，使首次命令也可记录状态。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)


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

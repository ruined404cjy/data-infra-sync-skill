"""稳定的结构化命令结果模型。"""

from dataclasses import fields, is_dataclass, dataclass
from typing import Any, Optional


REQUIRED_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "command",
        "state",
        "reason_codes",
        "target",
        "repositories",
        "changed",
        "next_actions",
        "snapshot",
        "stale_target",
    }
)


@dataclass(frozen=True)
class Action:
    """描述一个可由调用方执行的后续操作。"""

    kind: str
    argv: tuple[str, ...]
    mutates_worktree: bool
    requires_confirmation: bool
    preconditions: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    """描述命令执行状态及结构化的下一步操作。"""

    command: str
    state: str
    reason_codes: tuple[str, ...]
    target: Optional[dict[str, Any]]
    repositories: tuple[dict[str, Any], ...]
    changed: bool
    next_actions: tuple[Action, ...]
    snapshot: Optional[str]
    stale_target: Optional[bool]

    def to_dict(self) -> dict[str, object]:
        """返回符合 result-v1 schema 的原生 JSON 数据。"""
        return {
            "schema_version": "1",
            "command": self.command,
            "state": self.state,
            "reason_codes": _to_json(self.reason_codes),
            "target": _to_json(self.target),
            "repositories": _to_json(self.repositories),
            "changed": self.changed,
            "next_actions": _to_json(self.next_actions),
            "snapshot": self.snapshot,
            "stale_target": self.stale_target,
        }


def _to_json(value: Any) -> object:
    """递归转换 dataclass、元组和映射为 JSON 原生数据。"""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _to_json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: _to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json(item) for item in value]
    return value

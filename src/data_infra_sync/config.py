"""工作区配置的读取与优先级解析。"""

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


_CONFIG_DIRECTORY = "data-infra-sync-skill"
_DEFAULT_REMOTE = "origin"
_DEFAULT_BRANCH = "main"
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BRANCH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GIT_KEYS = {
    "root": "data-infra-sync.root",
    "target_remote": "data-infra-sync.targetremote",
    "target_branch": "data-infra-sync.targetbranch",
    "state_dir": "data-infra-sync.statedir",
}
_WRITE_GIT_KEYS = {
    "root": "data-infra-sync.root",
    "target_remote": "data-infra-sync.targetRemote",
    "target_branch": "data-infra-sync.targetBranch",
    "state_dir": "data-infra-sync.stateDir",
}
_ENV_KEYS = {
    "root": "DATA_INFRA_SYNC_ROOT",
    "target_remote": "DATA_INFRA_SYNC_TARGET_REMOTE",
    "target_branch": "DATA_INFRA_SYNC_TARGET_BRANCH",
    "state_dir": "DATA_INFRA_SYNC_STATE_DIR",
}


@dataclass(frozen=True)
class WorkspaceConfig:
    """描述一个 checkout 的同步配置及其状态目录。"""

    root: Path
    target_remote: str
    target_branch: str
    config_path: Path
    state_dir: Path


def write_config(config: WorkspaceConfig) -> None:
    """原子写入可由 Git 解析的规范工作区配置。"""
    values = (
        (_WRITE_GIT_KEYS["root"], str(config.root)),
        (_WRITE_GIT_KEYS["target_remote"], config.target_remote),
        (_WRITE_GIT_KEYS["target_branch"], config.target_branch),
        (_WRITE_GIT_KEYS["state_dir"], str(config.state_dir)),
    )
    if any("\n" in value or "\r" in value or "\0" in value for _, value in values):
        raise ValueError("config values must not contain newlines or NUL bytes")

    path = Path(config.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        for key, value in values:
            subprocess.run(
                ["git", "config", "--file", str(temporary), key, value],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        with temporary.open("rb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_config(
    cli: Mapping[str, str], environ: Mapping[str, str], path: Optional[Path]
) -> WorkspaceConfig:
    """按 CLI、环境、Git config 和默认值的优先级读取工作区配置。"""
    provisional_root = _resolve_path(
        _first_value(cli, environ, "root") or str(Path.cwd())
    )
    config_path = _resolve_config_path(path, environ, provisional_root)
    values = _read_git_config(config_path)

    root = _resolve_path(
        _first_value(cli, environ, "root")
        or values.get(_GIT_KEYS["root"])
        or str(Path.cwd())
    )
    target_remote = (
        _first_value(cli, environ, "target_remote")
        or values.get(_GIT_KEYS["target_remote"])
        or _DEFAULT_REMOTE
    )
    target_branch = (
        _first_value(cli, environ, "target_branch")
        or values.get(_GIT_KEYS["target_branch"])
        or _DEFAULT_BRANCH
    )
    state_dir_value = (
        _first_value(cli, environ, "state_dir")
        or values.get(_GIT_KEYS["state_dir"])
    )
    state_dir = (
        _resolve_path(state_dir_value)
        if state_dir_value
        else _default_state_dir(environ, root)
    )

    _validate_target_selection(target_remote, target_branch)
    return WorkspaceConfig(root, target_remote, target_branch, config_path, state_dir)


def _first_value(cli: Mapping[str, str], environ: Mapping[str, str], name: str) -> Optional[str]:
    """返回指定配置项的 CLI 或环境变量值。"""
    if name in cli:
        return cli[name]
    return environ.get(_ENV_KEYS[name])


def _validate_target_selection(remote: str, branch: str) -> None:
    """验证同步目标的 remote 名称与 Git 分支名称。"""
    if _REMOTE_NAME.fullmatch(remote) is None:
        raise ValueError("unsupported target remote")
    if any(_BRANCH_SEGMENT.fullmatch(part) is None for part in branch.split("/")):
        raise ValueError("unsupported target branch")
    completed = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("unsupported target branch")


def _resolve_config_path(path: Optional[Path], environ: Mapping[str, str], root: Path) -> Path:
    """返回显式配置文件或由临时 workspace key 定位的默认文件。"""
    if path is not None:
        return _resolve_path(str(path))
    config_home = _resolve_path(
        environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    )
    return config_home / _CONFIG_DIRECTORY / (_workspace_key(root) + ".conf")


def _default_state_dir(environ: Mapping[str, str], root: Path) -> Path:
    """返回由最终工作区规范路径隔离的默认状态目录。"""
    state_home = _resolve_path(
        environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    )
    return state_home / _CONFIG_DIRECTORY / _workspace_key(root)


def _workspace_key(root: Path) -> str:
    """为规范化工作区路径生成稳定且简短的目录键。"""
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def _resolve_path(value: str) -> Path:
    """展开用户目录并规范化可能尚不存在的路径。"""
    return Path(value).expanduser().resolve(strict=False)


def _read_git_config(path: Path) -> Mapping[str, str]:
    """通过 Git 解析指定配置文件的键值，保留最后一个同名值。"""
    if not path.exists():
        return {}
    completed = subprocess.run(
        ["git", "config", "--file", str(path), "--null", "--list"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    values = {}
    for entry in completed.stdout.decode("utf-8").split("\0"):
        if not entry:
            continue
        key, value = entry.split("\n", 1)
        values[key.lower()] = value
    return values

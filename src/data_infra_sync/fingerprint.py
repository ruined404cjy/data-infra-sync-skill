"""Git 领域状态的保守内容指纹。"""

import hashlib
import os
import stat
from pathlib import Path


def repository_fingerprint(git, repository):
    """返回单仓全部本地 refs、index、status 与工作树内容指纹。"""
    repository = Path(os.path.abspath(str(repository)))
    if not _safe_repository_path(repository):
        return ("missing",)
    head = git.run(repository, ("rev-parse", "HEAD")).stdout.strip()
    branch = git.run(
        repository, ("symbolic-ref", "--quiet", "HEAD"), check=False
    )
    refs = git.run(
        repository,
        ("for-each-ref", "--format=%(refname)%00%(objectname)", "refs"),
    ).stdout
    index = git.run(repository, ("ls-files", "--stage", "-z")).stdout
    status = git.run(
        repository,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
    ).stdout
    paths = git.run(
        repository,
        ("ls-files", "-z", "--cached", "--others", "--exclude-standard"),
    ).stdout
    files = tuple(
        (relative, _path_fingerprint(repository, relative))
        for relative in sorted(item for item in paths.split("\0") if item)
    )
    return (
        head,
        branch.returncode,
        branch.stdout,
        refs,
        index,
        status,
        files,
    )


def _safe_repository_path(repository):
    """通过 lstat 验证仓库入口及其路径组件，拒绝任何 symlink。"""
    current = Path(repository.anchor)
    metadata = current.lstat()
    for part in repository.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("repository path contains symlink")
        if current != repository and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("repository path component is not a directory")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("repository path is not a directory")
    return True


def _path_fingerprint(repository, relative):
    """不跟随 symlink 地计算单个工作树路径的稳定内容指纹。"""
    path = repository
    parts = Path(relative).parts
    for position, part in enumerate(parts):
        path = path / part
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return (
                "symlink",
                position,
                stat.S_IMODE(metadata.st_mode),
                os.readlink(path),
            )
    if stat.S_ISREG(metadata.st_mode):
        return (
            "file",
            stat.S_IMODE(metadata.st_mode),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    if stat.S_ISDIR(metadata.st_mode):
        return ("directory", stat.S_IMODE(metadata.st_mode))
    return ("other", metadata.st_mode, metadata.st_size)

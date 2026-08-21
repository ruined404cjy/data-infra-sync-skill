"""临时组合仓测试 fixture。"""

import subprocess
from pathlib import Path
from typing import Sequence


class CompositeFixture:
    """创建包含父仓、bare remote 和一级 submodule 的临时组合仓。"""

    def __init__(self, root: Path):
        self.root = root
        self.parent_remote = root / "parent-remote.git"
        self.submodule_remote = root / "submodule-remote.git"
        self.parent = root / "parent checkout"
        self.submodule = self.parent / "modules" / "component"
        self.target_parent = ""
        self.target_pin = ""

    @classmethod
    def create(cls, root: Path) -> "CompositeFixture":
        """初始化组合仓并返回可继续构造场景的 fixture。"""
        fixture = cls(root)
        root.mkdir(parents=True, exist_ok=True)
        fixture._run(root, ("init", "--bare", str(fixture.submodule_remote)))
        fixture._run(root, ("init", "--bare", str(fixture.parent_remote)))
        fixture._run(
            root,
            ("--git-dir", str(fixture.submodule_remote), "symbolic-ref", "HEAD", "refs/heads/main"),
        )
        fixture._run(
            root,
            ("--git-dir", str(fixture.parent_remote), "symbolic-ref", "HEAD", "refs/heads/main"),
        )

        source = root / "submodule source"
        fixture._run(root, ("init", "--initial-branch=main", str(source)))
        fixture._configure_user(source)
        fixture.commit_file(source, "README.md", "initial submodule\n", "initial submodule")
        fixture._run(source, ("remote", "add", "origin", str(fixture.submodule_remote)))
        fixture._run(source, ("push", "-u", "origin", "main"))

        fixture._run(root, ("clone", str(fixture.parent_remote), str(fixture.parent)))
        fixture._configure_user(fixture.parent)
        fixture._run(
            fixture.parent,
            (
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-b",
                "main",
                str(fixture.submodule_remote),
                "modules/component",
            ),
        )
        fixture._configure_user(fixture.submodule)
        fixture._run(fixture.parent, ("commit", "-m", "initial composite"))
        fixture._run(fixture.parent, ("push", "-u", "origin", "main"))
        fixture.target_parent = fixture.rev_parse(fixture.parent, "HEAD")
        fixture.target_pin = fixture.rev_parse(fixture.submodule, "HEAD")
        return fixture

    def commit_file(self, repo: Path, relative_path: str, contents: str, message: str) -> str:
        """写入、提交一个文件，并返回提交对象 ID。"""
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        self._run(repo, ("add", relative_path))
        self._run(repo, ("commit", "-m", message))
        return self.rev_parse(repo, "HEAD")

    def create_branch(self, repo: Path, name: str, start: str) -> None:
        """从指定提交创建本地分支。"""
        self._run(repo, ("branch", name, start))

    def switch(self, repo: Path, name: str) -> None:
        """切换到指定本地分支。"""
        self._run(repo, ("switch", name))

    def empty_commit(self, repo: Path, message: str) -> str:
        """创建不改变树的提交，并返回提交对象 ID。"""
        self._run(repo, ("commit", "--allow-empty", "-m", message))
        return self.rev_parse(repo, "HEAD")

    def make_dirty(self, repo: Path, relative_path: str = "dirty file.txt") -> None:
        """在仓库工作树写入未跟踪文件。"""
        (repo / relative_path).write_text("dirty\n", encoding="utf-8")

    def activate_operation(self, repo: Path, marker: str = "MERGE_HEAD") -> None:
        """写入 Git 操作标记，模拟待完成的 Git 操作。"""
        marker_path = Path(self._run(repo, ("rev-parse", "--git-path", marker)).stdout.strip())
        if not marker_path.is_absolute():
            marker_path = repo / marker_path
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(self.rev_parse(repo, "HEAD") + "\n", encoding="utf-8")

    def update_target_pin(self, contents: str = "target submodule\n") -> str:
        """推进 submodule 并提交父仓 gitlink，返回新的父仓目标提交。"""
        self.commit_file(self.submodule, "target.txt", contents, "advance submodule target")
        self._run(self.submodule, ("push", "origin", "main"))
        self._run(self.parent, ("add", "modules/component"))
        self._run(self.parent, ("commit", "-m", "advance composite target"))
        self._run(self.parent, ("push", "origin", "main"))
        self.target_parent = self.rev_parse(self.parent, "HEAD")
        self.target_pin = self.rev_parse(self.submodule, "HEAD")
        return self.target_parent

    def rev_parse(self, repo: Path, revision: str) -> str:
        """返回指定 revision 的完整对象 ID。"""
        return self._run(repo, ("rev-parse", revision)).stdout.strip()

    @staticmethod
    def _configure_user(repo: Path) -> None:
        """设置 fixture 提交所需的本地用户身份。"""
        CompositeFixture._run(repo, ("config", "user.name", "Fixture User"))
        CompositeFixture._run(repo, ("config", "user.email", "fixture@example.invalid"))

    @staticmethod
    def _run(repo: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """以 argv 调用 Git，并在 fixture 设置失败时立即终止测试。"""
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                "fixture Git command failed: {}\n{}".format(
                    " ".join(completed.args), completed.stderr
                )
            )
        return completed

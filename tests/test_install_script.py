"""跨 Agent Skill 安装脚本的行为测试。"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install-skill.sh"
CLI = ROOT / "scripts/data-infra-sync"
SKILL_NAME = "data-infra-sync-skill"
HOST_DIRECTORIES = {
    "codex": ".agents",
    "claude": ".claude",
    "gemini": ".gemini",
}


class InstallSkillScriptTests(unittest.TestCase):
    def run_installer(self, home: Path, *arguments: str) -> subprocess.CompletedProcess:
        """在隔离 HOME 中运行安装器并返回完整进程结果。"""
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        return subprocess.run(
            ["bash", str(INSTALLER), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def assert_empty_home(self, home: Path) -> None:
        """确认失败的参数解析没有产生任何 HOME 内容。"""
        self.assertEqual(list(home.iterdir()), [])

    def test_each_host_installs_only_its_standard_skill_link(self):
        """防止 host 映射错误或一次安装写入多个 Agent 目录。"""
        for host, host_directory in HOST_DIRECTORIES.items():
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)

                completed = self.run_installer(home, "--host", host)

                self.assertEqual(completed.returncode, 0, completed.stderr)
                skill_link = home / host_directory / "skills" / SKILL_NAME
                self.assertTrue(skill_link.is_symlink())
                self.assertEqual(skill_link.resolve(), ROOT.resolve())
                self.assertFalse((home / ".local").exists())
                for other_host, other_directory in HOST_DIRECTORIES.items():
                    if other_host != host:
                        self.assertFalse((home / other_directory).exists())

    def test_bin_flag_works_before_host_and_supports_spaces_in_home(self):
        """防止参数顺序或 HOME 空格破坏两个链接的创建。"""
        with tempfile.TemporaryDirectory(prefix="skill install home ") as temporary:
            home = Path(temporary)

            completed = self.run_installer(home, "--bin", "--host", "gemini")

            self.assertEqual(completed.returncode, 0, completed.stderr)
            skill_link = home / ".gemini/skills" / SKILL_NAME
            binary_link = home / ".local/bin/data-infra-sync"
            self.assertTrue(skill_link.is_symlink())
            self.assertEqual(skill_link.resolve(), ROOT.resolve())
            self.assertTrue(binary_link.is_symlink())
            self.assertEqual(binary_link.resolve(), CLI.resolve())
            self.assertTrue(os.access(binary_link, os.X_OK))

    def test_invalid_arguments_are_fully_validated_before_any_write(self):
        """防止缺失、非法、重复或尾随参数留下部分安装。"""
        cases = (
            (),
            ("--bin",),
            ("--host",),
            ("--host", "unknown"),
            ("--host", "codex", "--unknown"),
            ("--host", "codex", "--bin", "trailing"),
            ("--host", "codex", "--host", "claude"),
            ("--host", "codex", "--bin", "--bin"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary)

                completed = self.run_installer(home, *arguments)

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("Usage:", completed.stderr)
                self.assert_empty_home(home)

    def test_same_links_are_idempotent(self):
        """防止重复安装把已正确链接误判为冲突。"""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)

            first = self.run_installer(home, "--host", "claude", "--bin")
            second = self.run_installer(home, "--host", "claude", "--bin")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                (home / ".claude/skills" / SKILL_NAME).resolve(), ROOT.resolve()
            )
            self.assertEqual(
                (home / ".local/bin/data-infra-sync").resolve(), CLI.resolve()
            )

    def test_parent_file_conflict_is_preserved_before_any_install(self):
        """防止链接父路径冲突时留下另一目标的半安装。"""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            parent_conflict = home / ".local"
            parent_conflict.write_text("keep\n", encoding="utf-8")

            completed = self.run_installer(home, "--host", "codex", "--bin")

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(str(parent_conflict), completed.stderr)
            self.assertEqual(parent_conflict.read_text(encoding="utf-8"), "keep\n")
            self.assertFalse((home / ".agents").exists())

    def test_skill_file_conflict_is_preserved_without_partial_bin_install(self):
        """防止 Skill 目标冲突时覆盖文件或仍创建可执行链接。"""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            target = home / ".agents/skills" / SKILL_NAME
            target.parent.mkdir(parents=True)
            target.write_text("keep\n", encoding="utf-8")

            completed = self.run_installer(home, "--host", "codex", "--bin")

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(str(target), completed.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
            self.assertFalse(target.is_symlink())
            self.assertFalse((home / ".local").exists())

    def test_bin_symlink_conflict_is_preserved_without_partial_skill_install(self):
        """防止可执行目标冲突时替换链接或留下 Skill 半安装。"""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            other = home / "other-command"
            other.write_text("other\n", encoding="utf-8")
            binary_link = home / ".local/bin/data-infra-sync"
            binary_link.parent.mkdir(parents=True)
            binary_link.symlink_to(other)

            completed = self.run_installer(home, "--host", "gemini", "--bin")

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(str(binary_link), completed.stderr)
            self.assertTrue(binary_link.is_symlink())
            self.assertEqual(binary_link.resolve(), other.resolve())
            self.assertFalse((home / ".gemini").exists())


if __name__ == "__main__":
    unittest.main()

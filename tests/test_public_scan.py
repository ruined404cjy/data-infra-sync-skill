"""公开发布候选内容扫描器的行为测试。"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts/public-scan.sh"


class PublicScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="public scan ")
        self.repo = Path(self.temporary.name) / "candidate repo"
        self.home = Path(self.temporary.name) / "home with spaces"
        self.repo.mkdir()
        self.home.mkdir()
        self.git("init")
        self.git("config", "user.name", "Fixture User")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write("README.md", "# clean fixture\nhttps://user:pass@example.invalid/path\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "initial")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        """在 fixture 仓库运行 Git 并要求成功。"""
        return subprocess.run(
            ["git", *arguments], cwd=self.repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )

    def write(self, relative_path, contents):
        """写入 fixture 文件并返回路径。"""
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def run_scan(self, environment_changes=None):
        """使用带空格的隔离 HOME 扫描 fixture。"""
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        environment.update(environment_changes or {})
        return subprocess.run(
            ["bash", str(SCANNER), str(self.repo)], cwd=ROOT, env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def assert_finding(self, category, filename, forbidden_value=None):
        """确认扫描只报告类别和相对文件名，并保留 Git 状态。"""
        before = self.git("status", "--short", "--untracked-files=all").stdout
        head_before = self.git("rev-parse", "HEAD").stdout
        index_before = self.git("write-tree").stdout

        completed = self.run_scan()

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "{}: {}".format(category, filename))
        if forbidden_value is not None:
            self.assertNotIn(forbidden_value, completed.stdout + completed.stderr)
        self.assertEqual(self.git("status", "--short", "--untracked-files=all").stdout, before)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head_before)
        self.assertEqual(self.git("write-tree").stdout, index_before)

    def test_clean_repository_passes_and_invalid_example_url_is_allowed(self):
        """防止确定性 fixture 和干净候选仓库产生误报。"""
        self.write(
            "tests/fixture.py",
            'secret = "deterministic-fixture-value"\npassword = "not-a-real-password"\n',
        )
        self.git("add", "tests/fixture.py")
        self.git("commit", "-m", "add deterministic source fixture")
        before = self.git("status", "--short", "--untracked-files=all").stdout

        completed = self.run_scan()

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual((completed.stdout, completed.stderr), ("", ""))
        self.assertEqual(self.git("status", "--short", "--untracked-files=all").stdout, before)

    def test_current_home_paths_are_reported_without_echoing_the_path(self):
        """防止 Linux、macOS 或 Windows 个人 HOME 路径进入发布内容。"""
        cases = (
            str(self.home),
            "/Users/{}/project".format(self.home.name),
            "C:\\Users\\{}\\project".format(self.home.name),
        )
        for path_value in cases:
            with self.subTest(path=path_value):
                self.write("docs/path.txt", "checkout={}\n".format(path_value))
                self.git("add", "docs/path.txt")
                self.assert_finding("personal-path", "docs/path.txt", path_value)
                self.git("reset", "--hard", "HEAD")

    def test_credential_filename_and_content_are_reported_without_secret_values(self):
        """防止 credential 文件或高置信凭据内容进入发布并被扫描输出泄漏。"""
        secret = "qcc-secret-value-123456789"
        self.write("config/settings.ini", "api_key={}\n".format(secret))
        self.git("add", "config/settings.ini")
        self.assert_finding("credential", "config/settings.ini", secret)

        self.git("reset", "--hard", "HEAD")
        self.write("credentials.json", "{}\n")
        self.git("add", "credentials.json")
        self.assert_finding("credential", "credentials.json")

    def test_non_invalid_userinfo_url_is_reported_without_credentials(self):
        """防止包含 userinfo 的真实域 URL 进入公开候选内容。"""
        urls = (
            "https://" + "private-user:private-pass@example.com/repo.git",
            "https://" + "private-user@example.com/repo.git",
            "https://" + "private-user:@example.com/repo.git",
            "ssh://" + "private-user:private-pass@example.com/repo",
        )
        for secret_url in urls:
            with self.subTest(url=secret_url):
                self.write("config/remote.txt", secret_url + "\n")
                self.git("add", "config/remote.txt")
                self.assert_finding("userinfo-url", "config/remote.txt", secret_url)

    def test_invalid_userinfo_host_boundary_allows_punctuation_but_not_suffix(self):
        """防止 `.invalid` 文本边界误报或恶意后缀绕过 URL 扫描。"""
        self.write("docs/url.txt", 'remote="https://user:pass@fixture.invalid", next\n')
        self.git("add", "docs/url.txt")
        clean = self.run_scan()
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)

        self.write(
            "docs/url.txt", "https://" + "user:pass@fixture.invalid.evil/path\n"
        )
        self.git("add", "docs/url.txt")
        self.assert_finding("userinfo-url", "docs/url.txt", "user:pass")

    def test_invalid_userinfo_host_is_case_insensitive_and_accepts_dns_root_dot(self):
        """防止大小写或 DNS absolute name 使 `.invalid` fixture 误报。"""
        cases = (
            "https://user:pass@fixture.INVALID/path",
            "https://user:pass@fixture.invalid.?query=yes",
            "https://user:pass@fixture.invalid#fragment",
            "<https://user:pass@fixture.invalid>",
        )
        for url in cases:
            with self.subTest(url=url):
                self.write("docs/url.txt", 'remote="{}"\n'.format(url))
                self.git("add", "docs/url.txt")
                completed = self.run_scan()
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_tracked_content_is_scanned_from_index_not_worktree(self):
        """防止 staged credential 被未暂存的干净工作树内容遮蔽。"""
        secret = "staged-secret-value-12345"
        self.write("config/release.ini", "api_key={}\n".format(secret))
        self.git("add", "config/release.ini")
        self.write("config/release.ini", "safe=true\n")
        self.assert_finding("credential", "config/release.ini", secret)

    def test_staged_symlink_scans_link_blob_without_dereferencing(self):
        """防止 staged symlink 绕过个人路径扫描或读取链接目标。"""
        link = self.repo / "docs/home-link.txt"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.home)
        self.git("add", "docs/home-link.txt")
        self.assert_finding("personal-path", "docs/home-link.txt", str(self.home))

    def test_rg_failure_is_sanitized_and_exits_two(self):
        """防止内容读取工具失败被当成无匹配或泄漏诊断内容。"""
        shim_dir = Path(self.temporary.name) / "shim"
        shim_dir.mkdir()
        shim = shim_dir / "rg"
        leaked = "RG-SECRET-ABSOLUTE-{}".format(self.home)
        shim.write_text("#!/bin/sh\nprintf '%s\\n' \"$LEAK_VALUE\" >&2\nexit 2\n", encoding="utf-8")
        shim.chmod(0o755)

        completed = self.run_scan(
            {"PATH": str(shim_dir) + os.pathsep + os.environ["PATH"], "LEAK_VALUE": leaked}
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "scan-error: content-scan-failed\n")
        self.assertNotIn(leaked, completed.stderr)

    def test_local_log_or_state_file_in_candidate_set_is_reported(self):
        """防止本地审计日志或运行状态文件进入发布候选集。"""
        self.write("local/events.jsonl", "{}\n")
        self.git("add", "local/events.jsonl")
        self.assert_finding("local-artifact", "local/events.jsonl")

    def test_untracked_source_document_or_config_fragment_is_reported(self):
        """防止未跟踪的实现或文档片段在发布前遗漏。"""
        self.write("notes/new check.py", "print('unfinished')\n")
        self.assert_finding("untracked-source", "notes/new check.py")


if __name__ == "__main__":
    unittest.main()

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync import cli
from data_infra_sync.adapters.datainfra import (
    DataInfraAdapter,
    DataInfraInstallAdapter,
    ManagedPatch,
    _resolve_submodule_url,
)
from data_infra_sync.config import WorkspaceConfig, load_config, write_config
from data_infra_sync.executor import execute_sync
from data_infra_sync.git import Git, GitError
from data_infra_sync.planner import plan_sync, snapshot_for
from tests.git_fixture import CompositeFixture


_DEFAULT_PROC_READER = DataInfraInstallAdapter._read_proc


def setUpModule():
    """隔离组合仓测试，避免宿主 gaussdb procfs 权限影响 Git 事实断言。"""
    DataInfraInstallAdapter._read_proc = staticmethod(lambda: ())


def tearDownModule():
    DataInfraInstallAdapter._read_proc = staticmethod(_DEFAULT_PROC_READER)


class RecordingGit:
    """记录真实 Git 调用，同时保留完整子进程行为。"""

    def __init__(self):
        self.delegate = Git()
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def run(self, repo, args, *, check=True):
        self.calls.append((Path(repo).resolve(strict=False), tuple(args)))
        return self.delegate.run(repo, args, check=check)


def config_for(fixture, *, root=None, remote="origin", branch="main"):
    root = Path(root or fixture.parent).resolve()
    return WorkspaceConfig(
        root,
        remote,
        branch,
        fixture.root / "workspace.conf",
        fixture.root / "state",
    )


class DataInfraAdapterCollectionTest(unittest.TestCase):
    def test_proc_reader_skips_pid_disappearing_during_comm_exe_or_maps_read(self):
        """防止 procfs 竞态把已消失 PID 报为安装读取失败。"""
        processes = tuple(Path("/proc") / pid for pid in ("101", "102", "103"))

        def read_text(path):
            if path.name == "comm" and path.parent.name == "101":
                raise FileNotFoundError(errno.ENOENT, "gone")
            if path.name == "comm" and path.parent.name == "102":
                return "gaussdb\n"
            if path.name == "maps" and path.parent.name == "103":
                raise FileNotFoundError(errno.ENOENT, "gone")
            return "gaussdb\n"

        def readlink(path):
            if path.parent.name == "102":
                raise FileNotFoundError(errno.ENOENT, "gone")
            return "/checkout/bin/gaussdb"

        with mock.patch("data_infra_sync.adapters.datainfra.Path.iterdir", return_value=processes), \
             mock.patch("data_infra_sync.adapters.datainfra._read_proc_text", side_effect=read_text), \
             mock.patch("data_infra_sync.adapters.datainfra.os.readlink", side_effect=readlink):
            self.assertEqual(_DEFAULT_PROC_READER(), ())

    def test_proc_reader_does_not_read_exe_or_maps_for_non_gaussdb(self):
        """防止非 gaussdb 进程触发不必要的 procfs 读取。"""
        process = Path("/proc/201")
        with mock.patch("data_infra_sync.adapters.datainfra.Path.iterdir", return_value=(process,)), \
             mock.patch("data_infra_sync.adapters.datainfra._read_proc_text", return_value="postgres\n") as read_text, \
             mock.patch("data_infra_sync.adapters.datainfra.os.readlink") as readlink:
            self.assertEqual(_DEFAULT_PROC_READER(), ())
            readlink.assert_not_called()
            read_text.assert_called_once_with(process / "comm")

    def test_proc_reader_propagates_permission_errors_from_comm_exe_and_maps(self):
        """防止 procfs 权限错误被静默降级为无进程。"""
        for field, error in (
            ("comm", PermissionError(errno.EPERM, "denied")),
            ("exe", PermissionError(errno.EPERM, "denied")),
            ("maps", PermissionError(errno.EACCES, "denied")),
        ):
            with self.subTest(field=field):
                process = Path("/proc/301")

                def read_text(path, field=field, error=error):
                    if path.name == field:
                        raise error
                    return "gaussdb\n"

                def readlink(path, field=field, error=error):
                    if field == "exe":
                        raise error
                    return "/checkout/bin/gaussdb"

                with mock.patch("data_infra_sync.adapters.datainfra.Path.iterdir", return_value=(process,)), \
                     mock.patch("data_infra_sync.adapters.datainfra._read_proc_text", side_effect=read_text), \
                     mock.patch("data_infra_sync.adapters.datainfra.os.readlink", side_effect=readlink):
                    with self.assertRaises(type(error)) as raised:
                        _DEFAULT_PROC_READER()
                    self.assertEqual(raised.exception.errno, error.errno)

    def test_uninitialized_empty_submodule_directory_is_missing_not_parent_repo(self):
        """防止空目录中的 Git 命令向上发现父仓并在错误对象库 fetch。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            fixture._run(
                fixture.parent,
                ("submodule", "deinit", "--force", "--", "modules/component"),
            )
            git = RecordingGit()
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)

            facts = adapter.collect_plan_facts(git, fresh=True)

            repository = facts.repositories[0]
            self.assertEqual(repository.facts.worktree, "missing")
            empty = fixture.submodule.resolve(strict=False)
            self.assertFalse(
                any(
                    repo == empty and args and args[0] == "fetch"
                    for repo, args in git.calls
                )
            )

    def test_exact_target_fetch_disables_recursive_submodule_fetch(self):
        """防止 exact pin 预取继承递归配置并访问嵌套 remote。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            modules = fixture.parent / ".gitmodules"
            modules.write_text(
                modules.read_text(encoding="utf-8").replace(
                    str(fixture.submodule_remote), "origin"
                ),
                encoding="utf-8",
            )
            fixture._run(fixture.parent, ("add", ".gitmodules"))
            fixture._run(fixture.parent, ("commit", "-m", "use named child remote"))
            fixture.push(fixture.parent)
            nested_remote = fixture.root / "nested.git"
            fixture._run(fixture.root, ("init", "--bare", str(nested_remote)))
            fixture._run(
                fixture.root,
                ("--git-dir", str(nested_remote), "symbolic-ref", "HEAD", "refs/heads/main"),
            )
            nested_source = fixture.root / "nested source"
            fixture._run(
                fixture.root, ("init", "--initial-branch=main", str(nested_source))
            )
            fixture._configure_user(nested_source)
            fixture.commit_file(nested_source, "README.md", "nested\n", "nested initial")
            fixture._run(nested_source, ("remote", "add", "origin", str(nested_remote)))
            fixture._run(nested_source, ("push", "-u", "origin", "main"))
            fixture._run(
                fixture.submodule,
                (
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(nested_remote),
                    "nested/child",
                ),
            )
            fixture._run(fixture.submodule, ("commit", "-m", "add nested child"))
            fixture.push(fixture.submodule)
            fixture._run(fixture.parent, ("add", "modules/component"))
            fixture._run(fixture.parent, ("commit", "-m", "record nested child"))
            fixture.push(fixture.parent)

            child_publisher = fixture.root / "child publisher"
            fixture._run(
                fixture.root,
                ("clone", str(fixture.submodule_remote), str(child_publisher)),
            )
            fixture._configure_user(child_publisher)
            fixture._run(
                child_publisher,
                ("-c", "protocol.file.allow=always", "submodule", "update", "--init"),
            )
            nested_child = child_publisher / "nested/child"
            fixture._configure_user(nested_child)
            fixture.commit_file(
                nested_child, "unpublished.txt", "private\n", "unpublished nested pin"
            )
            fixture._run(child_publisher, ("add", "nested/child"))
            fixture._run(child_publisher, ("commit", "-m", "advance nested pin"))
            target_pin = fixture.rev_parse(child_publisher, "HEAD")
            fixture.push(child_publisher)
            parent_publisher = fixture.clone_parent("parent publisher")
            fixture._run(
                parent_publisher,
                (
                    "update-index",
                    "--cacheinfo",
                    "160000,{},modules/component".format(target_pin),
                ),
            )
            fixture._run(parent_publisher, ("commit", "-m", "advance child pin"))
            fixture.push(parent_publisher)
            fixture.detach(fixture.submodule)
            fixture.create_branch(fixture.submodule, "victim", "HEAD")
            fixture._run(
                fixture.submodule,
                (
                    "config",
                    "--replace-all",
                    "remote.origin.fetch",
                    "+refs/heads/main:refs/remotes/origin/main",
                ),
            )
            fixture._run(
                fixture.submodule,
                (
                    "config",
                    "--add",
                    "remote.origin.fetch",
                    "+refs/heads/main:refs/heads/victim",
                ),
            )
            fixture._run(fixture.submodule, ("config", "fetch.recurseSubmodules", "true"))
            git = RecordingGit()
            child_heads = self._heads(fixture.submodule)

            facts = DataInfraAdapter.for_workspace(
                config_for(fixture), git
            ).collect_plan_facts(git, fresh=True)

            self.assertEqual(facts.target_submodules[0].pin, target_pin)
            exact_fetches = [
                args for _, args in git.calls if args[:2] == ("fetch", "--no-tags")
            ]
            self.assertTrue(exact_fetches)
            self.assertTrue(
                all("--no-recurse-submodules" in args for args in exact_fetches)
            )
            self.assertEqual(self._heads(fixture.submodule), child_heads)
            self.assertTrue(all("--refmap=" in args for args in exact_fetches))

    def test_fresh_fetch_ignores_remote_refspecs_that_write_local_heads(self):
        """防止恶意 remote.fetch 在计划阶段改写父仓或子仓本地分支。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            initial_parent = fixture.target_parent
            initial_pin = fixture.target_pin
            fixture.create_branch(fixture.parent, "victim", initial_parent)
            fixture.create_branch(fixture.submodule, "victim", initial_pin)
            target = fixture.update_target_pin()
            target_pin = fixture.target_pin
            for repository, initial in (
                (fixture.parent, initial_parent),
                (fixture.submodule, initial_pin),
            ):
                fixture._run(repository, ("reset", "--hard", initial))
                fixture._run(
                    repository,
                    ("update-ref", "refs/remotes/origin/main", initial),
                )

            malicious = "+refs/heads/main:refs/heads/victim"
            for repository in (fixture.parent, fixture.submodule):
                fixture._run(
                    repository,
                    (
                        "config",
                        "--replace-all",
                        "remote.origin.fetch",
                        "+refs/heads/main:refs/remotes/origin/main",
                    ),
                )
                fixture._run(
                    repository,
                    ("config", "--add", "remote.origin.fetch", malicious),
                )
                fixture._run(
                    repository,
                    (
                        "symbolic-ref",
                        "refs/remotes/origin/main",
                        "refs/heads/victim",
                    ),
                )
            parent_heads = self._heads(fixture.parent)
            child_heads = self._heads(fixture.submodule)
            git = RecordingGit()

            facts = DataInfraAdapter.for_workspace(
                config_for(fixture), git
            ).collect_plan_facts(git, fresh=True)

            self.assertEqual(facts.target_parent, target)
            self.assertEqual(
                fixture.rev_parse(fixture.parent, "refs/remotes/origin/main"), target
            )
            self.assertEqual(
                fixture.rev_parse(fixture.submodule, "refs/remotes/origin/main"),
                target_pin,
            )
            self.assertEqual(self._heads(fixture.parent), parent_heads)
            self.assertEqual(self._heads(fixture.submodule), child_heads)
            for repository in (fixture.parent, fixture.submodule):
                symbolic = git.run(
                    repository,
                    ("symbolic-ref", "--quiet", "refs/remotes/origin/main"),
                    check=False,
                )
                self.assertEqual(symbolic.returncode, 1)
            fetches = [args for _, args in git.calls if args and args[0] == "fetch"]
            self.assertTrue(fetches)
            self.assertTrue(all("--refmap=" in args for args in fetches))

    def test_offline_uses_local_target_without_fetch_and_fresh_fetches_before_target(self):
        """防止 offline 访问网络，并防止 fresh 使用 fetch 前的目标引用。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            publisher = fixture.clone_parent("publisher")
            target = fixture.commit_file(
                publisher, "remote-target.txt", "advanced\n", "advance target"
            )
            fixture.push(publisher)
            git = RecordingGit()
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)

            offline = adapter.collect_plan_facts(git, fresh=False)
            self.assertTrue(offline.stale_target)
            self.assertEqual(offline.target_parent, offline.current_parent)
            self.assertFalse(any(args and args[0] == "fetch" for _, args in git.calls))

            git.calls.clear()
            fresh = adapter.collect_plan_facts(git, fresh=True)

            self.assertFalse(fresh.stale_target)
            self.assertEqual(fresh.target_parent, target)
            self.assertEqual(fresh.parent.behind, 1)
            fetches = [args for _, args in git.calls if args and args[0] == "fetch"]
            tracking_fetch = (
                "fetch",
                "--no-recurse-submodules",
                "--refmap=",
                "--",
                "origin",
                "refs/heads/main",
            )
            self.assertEqual(fetches[0], tracking_fetch)
            self.assertEqual(fetches[1], tracking_fetch)
            parent_fetch = next(
                index
                for index, (repo, args) in enumerate(git.calls)
                if repo == fixture.parent.resolve() and args and args[0] == "fetch"
            )
            current_head = next(
                index
                for index, (repo, args) in enumerate(git.calls)
                if repo == fixture.parent.resolve() and args == ("rev-parse", "HEAD")
            )
            self.assertLess(parent_fetch, current_head)
            target_read = next(
                index
                for index, (_, args) in enumerate(git.calls)
                if args[:2] == ("show-ref", "--verify")
            )
            self.assertLess(
                max(
                    index
                    for index, (_, args) in enumerate(git.calls)
                    if args and args[0] == "fetch"
                ),
                target_read,
            )

    def test_baseline_facts_are_complete_sorted_and_use_logical_paths(self):
        """防止采集器遗漏组合仓 pin、关系或泄漏绝对路径到计划模型。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            git = Git()
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)

            facts = adapter.collect_plan_facts(git, fresh=False)

            self.assertEqual(facts.current_parent, fixture.target_parent)
            self.assertEqual(facts.target_parent, fixture.target_parent)
            self.assertEqual(facts.parent_relation, "equal")
            self.assertEqual(facts.required_parent_branch, "main")
            self.assertEqual(
                tuple(item.path for item in facts.current_submodules),
                ("modules/component",),
            )
            self.assertEqual(facts.current_submodules, facts.target_submodules)
            repository = facts.repositories[0]
            self.assertEqual(repository.path, "modules/component")
            self.assertEqual(repository.current_pin, fixture.target_pin)
            self.assertEqual(repository.target_pin, fixture.target_pin)
            self.assertEqual(repository.relation, "equal")
            self.assertEqual(repository.managed_patch_state, "none")
            self.assertEqual(plan_sync(facts).state, "up_to_date")

    def test_parent_dirty_ignores_only_unstaged_gitlink_drift(self):
        """防止已暂存 gitlink 更新绕过父仓 dirty 阻塞。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            fixture.commit_file(
                fixture.submodule, "local-child.txt", "local\n", "local child"
            )
            git = Git()
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)

            unstaged = adapter.collect_plan_facts(git, fresh=False)
            self.assertFalse(unstaged.parent_non_submodule_dirty)
            self.assertFalse(unstaged.parent.index_dirty)

            fixture.stage(fixture.parent, "modules/component")
            staged = adapter.collect_plan_facts(git, fresh=False)
            self.assertTrue(staged.parent_non_submodule_dirty)
            self.assertTrue(staged.parent.index_dirty)
            self.assertEqual(plan_sync(staged).state, "blocked")

            fixture.commit_file(
                fixture.submodule, "second-child.txt", "second\n", "second child"
            )
            mixed = adapter.collect_plan_facts(git, fresh=False)
            self.assertTrue(mixed.parent_non_submodule_dirty)
            self.assertTrue(mixed.parent.index_dirty)
            self.assertTrue(mixed.parent.worktree_dirty)
            self.assertEqual(plan_sync(mixed).state, "blocked")

            fixture.make_dirty(fixture.parent, "ordinary.txt")
            ordinary = adapter.collect_plan_facts(git, fresh=False)
            self.assertTrue(ordinary.parent_non_submodule_dirty)

            (fixture.parent / "ordinary.txt").unlink()
            modules = fixture.parent / ".gitmodules"
            modules.write_text(
                modules.read_text(encoding="utf-8") + "# staged ordinary change\n",
                encoding="utf-8",
            )
            fixture.stage(fixture.parent, ".gitmodules")
            staged = adapter.collect_plan_facts(git, fresh=False)
            self.assertTrue(staged.parent_non_submodule_dirty)

    def test_current_workspace_gaussdb_and_nested_submodule_are_visible(self):
        """防止运行实例或一级子仓内的嵌套声明绕过计划阻塞。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            nested_remote = fixture.root / "nested.git"
            fixture._run(fixture.root, ("init", "--bare", str(nested_remote)))
            fixture._run(
                fixture.root,
                ("--git-dir", str(nested_remote), "symbolic-ref", "HEAD", "refs/heads/main"),
            )
            nested_source = fixture.root / "nested source"
            fixture._run(
                fixture.root, ("init", "--initial-branch=main", str(nested_source))
            )
            fixture._configure_user(nested_source)
            fixture.commit_file(nested_source, "README.md", "nested\n", "nested initial")
            fixture._run(nested_source, ("remote", "add", "origin", str(nested_remote)))
            fixture._run(nested_source, ("push", "-u", "origin", "main"))
            fixture._run(
                fixture.submodule,
                (
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(nested_remote),
                    "nested/child",
                ),
            )
            process_reader = lambda: (
                {
                    "name": "gaussdb",
                    "exe": str(fixture.parent / "mppdb_temp_install/bin/gaussdb"),
                    "maps": (),
                },
            )
            git = Git()
            adapter = DataInfraAdapter.for_workspace(
                config_for(fixture), git, process_reader=process_reader
            )

            facts = adapter.collect_plan_facts(git, fresh=False)

            self.assertTrue(facts.running_instances)
            self.assertTrue(facts.nested_submodules)
            self.assertEqual(
                plan_sync(facts).reason_codes,
                ("dirty_worktree", "running_instances", "unsupported_nested_submodule"),
            )

    def test_invalid_ref_and_unsafe_submodule_declaration_raise_git_error(self):
        """防止配置和提交声明进入不明确或越界的 Git 读取。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            git = Git()
            invalid = DataInfraAdapter.for_workspace(
                config_for(fixture, remote="--upload-pack=evil"), git
            )
            with self.assertRaises(GitError):
                invalid.collect_plan_facts(git, fresh=False)

            modules = (fixture.parent / ".gitmodules").read_text(encoding="utf-8")
            (fixture.parent / ".gitmodules").write_text(
                modules.replace("modules/component", "../escape"), encoding="utf-8"
            )
            fixture._run(fixture.parent, ("add", ".gitmodules"))
            fixture._run(fixture.parent, ("commit", "-m", "unsafe declaration"))
            unsafe = DataInfraAdapter.for_workspace(config_for(fixture), git)
            with self.assertRaises(GitError):
                unsafe.collect_plan_facts(git, fresh=False)

    def test_same_repository_snapshot_does_not_depend_on_checkout_path(self):
        """防止绝对 checkout 路径进入 snapshot。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            second = fixture.clone_parent("second checkout")
            fixture._run(
                second,
                ("-c", "protocol.file.allow=always", "submodule", "update", "--init"),
            )
            fixture.switch(second / "modules/component", "main")
            git = Git()

            first_facts = DataInfraAdapter.for_workspace(
                config_for(fixture), git
            ).collect_plan_facts(git, fresh=False)
            second_facts = DataInfraAdapter.for_workspace(
                config_for(fixture, root=second), git
            ).collect_plan_facts(git, fresh=False)

            self.assertEqual(snapshot_for(first_facts), snapshot_for(second_facts))

    @staticmethod
    def _heads(repository):
        return CompositeFixture._run(
            repository,
            ("for-each-ref", "--format=%(refname)%00%(objectname)%00", "refs/heads"),
        ).stdout.encode("utf-8")


class SubmoduleUrlTest(unittest.TestCase):
    def test_relative_urls_follow_git_remote_semantics(self):
        """防止 URL query/fragment 或协议差异改变相对 submodule 目标。"""
        root = Path("/srv/worktree")
        cases = (
            (
                "https://example.invalid/org/parent.git?token=x#fragment",
                "../child.git",
                "https://example.invalid/org/child.git",
            ),
            (
                "file:///srv/org/parent.git?token=x#fragment",
                "../child.git",
                "file:///srv/org/child.git",
            ),
            ("/srv/org/parent.git", "../child.git", "/srv/org/child.git"),
            ("../parent.git", "../child.git", "/srv/child.git"),
            (
                "git@example.invalid:org/parent.git",
                "../child.git",
                "git@example.invalid:org/child.git",
            ),
        )

        for parent, child, expected in cases:
            with self.subTest(parent=parent):
                self.assertEqual(
                    _resolve_submodule_url(root, parent, child), expected
                )


class DataInfraAdapterLayoutTest(unittest.TestCase):
    def test_current_and_target_accept_section_keyword_case_and_escaped_names(self):
        """防止 section 关键字大小写改变 name 的原始 quoted 语义。"""
        cases = (
            ('[Submodule "component"]', "component"),
            (
                r'[submodule "component\"quoted\\path"]',
                'component"quoted\\path',
            ),
        )
        for side in ("current", "target"):
            for header, expected_name in cases:
                with self.subTest(side=side, header=header):
                    with tempfile.TemporaryDirectory() as temp_dir:
                        fixture = CompositeFixture.create(Path(temp_dir))
                        repository = (
                            fixture.parent
                            if side == "current"
                            else fixture.clone_parent("publisher")
                        )
                        modules = repository / ".gitmodules"
                        lines = modules.read_text(encoding="utf-8").splitlines()
                        lines[0] = header
                        modules.write_text(
                            "\n".join(lines) + "\n",
                            encoding="utf-8",
                        )
                        fixture._run(repository, ("add", ".gitmodules"))
                        fixture._run(
                            repository, ("commit", "-m", "quote section name")
                        )
                        if side == "target":
                            fixture.push(repository)
                            fixture.fetch(fixture.parent)

                        facts = DataInfraAdapter.for_workspace(
                            config_for(fixture), Git()
                        ).collect_plan_facts(Git(), fresh=False)

                        specs = (
                            facts.current_submodules
                            if side == "current"
                            else facts.target_submodules
                        )
                        self.assertEqual(specs[0].name, expected_name)

    def test_current_and_target_reject_incomplete_or_duplicate_gitmodules_fields(self):
        """防止任一父提交的空、缺失或重复声明被部分解析。"""
        cases = (
            "empty-name",
            "empty-url",
            "duplicate-path",
            "duplicate-url",
            "missing-path",
            "missing-url",
            "split-section",
        )
        for side in ("current", "target"):
            for case in cases:
                with self.subTest(side=side, case=case), tempfile.TemporaryDirectory() as temp_dir:
                    fixture = CompositeFixture.create(Path(temp_dir))
                    repository = (
                        fixture.parent
                        if side == "current"
                        else fixture.clone_parent("publisher")
                    )
                    self._malform_gitmodules(repository / ".gitmodules", case)
                    fixture._run(repository, ("add", ".gitmodules"))
                    fixture._run(repository, ("commit", "-m", case))
                    if side == "target":
                        fixture.push(repository)
                        fixture.fetch(fixture.parent)
                    git = Git()

                    with self.assertRaises(GitError):
                        DataInfraAdapter.for_workspace(
                            config_for(fixture), git
                        ).collect_plan_facts(git, fresh=False)

    def test_target_add_is_visible_as_missing_repository_and_safe_update(self):
        """防止新增 submodule 被遗漏或误判为布局迁移。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            publisher = fixture.clone_parent("publisher")
            fixture._run(
                publisher,
                (
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(fixture.submodule_remote),
                    "modules/added",
                ),
            )
            fixture._run(
                publisher,
                (
                    "config",
                    "--file",
                    ".gitmodules",
                    "submodule.modules/added.url",
                    "../submodule-remote.git",
                ),
            )
            fixture._run(publisher, ("add", ".gitmodules"))
            fixture._run(publisher, ("commit", "-m", "add target submodule"))
            fixture.push(publisher)
            fixture._run(
                fixture.parent,
                ("config", "remote.origin.url", "../parent-remote.git"),
            )
            git = Git()

            facts = DataInfraAdapter.for_workspace(
                config_for(fixture), git
            ).collect_plan_facts(git, fresh=True)

            self.assertEqual(
                tuple(item.path for item in facts.target_submodules),
                ("modules/added", "modules/component"),
            )
            added = next(item for item in facts.repositories if item.path == "modules/added")
            self.assertEqual(added.facts.worktree, "missing")
            self.assertEqual(added.current_pin, None)
            self.assertEqual(added.relation, "not_applicable")
            self.assertEqual(plan_sync(facts).state, "update_ready")

    def test_target_remove_and_url_change_are_visible_layout_transitions(self):
        """防止删除或 URL 变化被误判为可自动同步。"""
        for change in ("remove", "url"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp_dir:
                fixture = CompositeFixture.create(Path(temp_dir))
                publisher = fixture.clone_parent("publisher")
                if change == "remove":
                    fixture._run(publisher, ("rm", "-f", "modules/component"))
                else:
                    modules = (publisher / ".gitmodules").read_text(encoding="utf-8")
                    (publisher / ".gitmodules").write_text(
                        modules.replace(str(fixture.submodule_remote), "../other.git"),
                        encoding="utf-8",
                    )
                    fixture._run(publisher, ("add", ".gitmodules"))
                fixture._run(publisher, ("commit", "-m", change + " target submodule"))
                fixture.push(publisher)
                git = Git()

                facts = DataInfraAdapter.for_workspace(
                    config_for(fixture), git
                ).collect_plan_facts(git, fresh=True)

                self.assertIn(
                    "submodule_layout_transition_required",
                    plan_sync(facts).reason_codes,
                )

    def test_existing_submodule_missing_target_object_raises_git_error(self):
        """防止缺失 target pin 对象被伪装成安全的关系状态。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            publisher = fixture.clone_parent("publisher")
            fixture._run(
                publisher,
                ("-c", "protocol.file.allow=always", "submodule", "update", "--init"),
            )
            child = publisher / "modules/component"
            fixture._configure_user(child)
            fixture.commit_file(child, "unpublished.txt", "local only\n", "unpublished pin")
            fixture._run(publisher, ("add", "modules/component"))
            fixture._run(publisher, ("commit", "-m", "point at unpublished pin"))
            fixture.push(publisher)
            git = Git()

            with self.assertRaises(GitError):
                DataInfraAdapter.for_workspace(
                    config_for(fixture), git
                ).collect_plan_facts(git, fresh=True)

    def test_new_submodule_unpublished_target_object_raises_git_error(self):
        """防止新增 submodule 的临时预取缺失时信任父仓 gitlink。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            publisher = fixture.clone_parent("publisher")
            unpublished = fixture.root / "unpublished child"
            fixture._run(
                fixture.root, ("clone", str(fixture.submodule_remote), str(unpublished))
            )
            fixture._configure_user(unpublished)
            fixture.commit_file(unpublished, "private.txt", "not pushed\n", "private pin")
            fixture._run(
                publisher,
                (
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(unpublished),
                    "modules/unpublished",
                ),
            )
            modules = (publisher / ".gitmodules").read_text(encoding="utf-8")
            (publisher / ".gitmodules").write_text(
                modules.replace(str(unpublished), str(fixture.submodule_remote)),
                encoding="utf-8",
            )
            fixture._run(publisher, ("add", ".gitmodules"))
            fixture._run(publisher, ("commit", "-m", "add unpublished target"))
            fixture.push(publisher)
            git = Git()

            with self.assertRaises(GitError):
                DataInfraAdapter.for_workspace(
                    config_for(fixture), git
                ).collect_plan_facts(git, fresh=True)

    @staticmethod
    def _malform_gitmodules(path, case):
        lines = path.read_text(encoding="utf-8").splitlines()
        if case == "empty-name":
            lines[0] = '[submodule ""]'
        elif case == "empty-url":
            lines = ["\turl =" if line.strip().startswith("url =") else line for line in lines]
        elif case == "duplicate-path":
            lines.append("\tpath = modules/component")
        elif case == "duplicate-url":
            url = next(line for line in lines if line.strip().startswith("url ="))
            lines.append(url)
        elif case == "missing-path":
            lines = [line for line in lines if not line.strip().startswith("path =")]
        elif case == "missing-url":
            lines = [line for line in lines if not line.strip().startswith("url =")]
        elif case == "split-section":
            url_index = next(
                index
                for index, line in enumerate(lines)
                if line.strip().startswith("url =")
            )
            lines.insert(url_index, lines[0])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class DataInfraAdapterManagedPatchTest(unittest.TestCase):
    def test_parent_merge_failure_hands_off_partial_without_resume(self):
        """父仓 merge 失败后交接现场，后续同步保持阻塞。"""
        with tempfile.TemporaryDirectory(prefix="managed patch partial handoff ") as temp_dir:
            fixture, patch_content = self._delta_fixture(Path(temp_dir))
            self._advance_parent_only(fixture)
            self._apply_patch(fixture, patch_content)
            config = config_for(fixture)
            class FailParentMergeOnce:
                def __init__(self):
                    self.delegate = Git()
                    self.failed = False

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def run(self, repo, args, *, check=True):
                    if args[:2] == ("merge", "--ff-only") and not self.failed:
                        self.failed = True
                        raise GitError(("git",) + tuple(args), "injected parent failure", 1)
                    return self.delegate.run(repo, args, check=check)

            git = FailParentMergeOnce()
            result = execute_sync(git, DataInfraAdapter.for_workspace(config, git), None, True)

            self.assertEqual((result.state, cli._exit_code(result)), ("partial", 4))
            self.assertEqual(result.reason_codes, ("parent_update_failed",))
            self.assertEqual(result.next_actions, ())
            self.assertFalse(
                (config.state_dir / ("managed-patch-" + "recovery.json")).exists()
            )

            next_git = Git()
            blocked = execute_sync(
                next_git,
                DataInfraAdapter.for_workspace(config, next_git),
                None,
                True,
            )
            self.assertEqual(
                (blocked.state, blocked.reason_codes),
                ("blocked", ("managed_patch_transition_required",)),
            )

    def test_target_exact_pin_with_equivalent_patch_content_finishes_clean(self):
        """防止目标已等价包含补丁时后置 clean 状态被误报为 partial。"""
        with tempfile.TemporaryDirectory(prefix="managed patch equivalent ") as temp_dir:
            fixture, patch_content = self._delta_fixture(Path(temp_dir))
            self._apply_patch(fixture, patch_content)
            target_parent, target_pin = self._advance_target_with_patch(fixture)
            git = Git()
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)

            result = execute_sync(git, adapter, None, True)

            self.assertEqual((result.state, result.changed), ("updated", True))
            self.assertEqual(cli._exit_code(result), 0)
            self.assertEqual(fixture.rev_parse(fixture.parent, "HEAD"), target_parent)
            self.assertEqual(fixture.rev_parse(fixture.submodule, "HEAD"), target_pin)
            self.assertEqual(
                fixture._run(
                    fixture.submodule,
                    ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                ).stdout,
                "",
            )
            self.assertEqual(
                (fixture.submodule / "README.md").read_text(encoding="utf-8"),
                "patched submodule\n",
            )

    def test_patch_target_outside_repository_union_sets_global_transition(self):
        """防止目标声明 Delta 补丁但缺少 Delta submodule 时生成同步 action。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            publisher = fixture.clone_parent("publisher")
            patch_path = publisher / "build/patches/iceberg-delta-cmake-pie-filter.patch"
            patch_path.parent.mkdir(parents=True)
            patch_path.write_text("unbound patch\n", encoding="utf-8")
            fixture._run(publisher, ("add", "build/patches"))
            fixture._run(publisher, ("commit", "-m", "declare unbound delta patch"))
            fixture.push(publisher)
            git = Git()

            facts = DataInfraAdapter.for_workspace(
                config_for(fixture), git
            ).collect_plan_facts(git, fresh=True)
            result = plan_sync(facts)

            self.assertTrue(facts.managed_patch_transition)
            self.assertEqual(result.state, "blocked")
            self.assertIn("managed_patch_transition_required", result.reason_codes)
            self.assertEqual(result.next_actions, ())

    def test_patch_loader_preserves_commit_blob_bytes(self):
        """防止补丁读取经过文本解码后改变原始字节与内容哈希。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = CompositeFixture.create(Path(temp_dir))
            content = b"binary-boundary:\xff\x00\n"
            patch_path = fixture.parent / "build/patches/iceberg-delta-cmake-pie-filter.patch"
            patch_path.parent.mkdir(parents=True)
            patch_path.write_bytes(content)
            fixture._run(fixture.parent, ("add", "build/patches"))
            fixture._run(fixture.parent, ("commit", "-m", "record raw patch blob"))
            commit = fixture.rev_parse(fixture.parent, "HEAD")
            git = Git()
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)

            decoy = fixture.root / "decoy"
            decoy.mkdir()
            fixture._run(decoy, ("init", "--initial-branch=main"))
            fixture._configure_user(decoy)
            fixture.commit_file(decoy, "README.md", "decoy\n", "decoy")
            redirected = {
                "GIT_DIR": str(decoy / ".git"),
                "GIT_WORK_TREE": str(decoy),
                "GIT_CONFIG_PARAMETERS": "'core.bare=true'",
            }

            with mock.patch.dict(os.environ, redirected, clear=False):
                patches = adapter.managed_patches(commit)

            self.assertEqual(len(patches), 1)
            self.assertEqual(patches[0].content, content)

    def test_unchanged_exact_dirty_patch_is_continuous(self):
        """防止精确受管 dirty 在目标可接纳时被错误阻塞。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture, patch_content = self._delta_fixture(Path(temp_dir))
            publisher = fixture.clone_parent("publisher")
            fixture.commit_file(publisher, "target.txt", "advance\n", "advance parent")
            fixture.push(publisher)
            self._apply_patch(fixture, patch_content)
            git = Git()
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)

            facts = adapter.collect_plan_facts(git, fresh=True)

            delta = facts.repositories[0]
            self.assertEqual(delta.path, "plugins/iceberg_delta")
            self.assertEqual(delta.managed_patch_state, "continuous")
            self.assertEqual(len(adapter.managed_patches(facts.current_parent)), 1)
            self.assertEqual(plan_sync(facts).state, "update_ready")

    def test_multiple_unchanged_declarations_block_before_domain_writes(self):
        """防止多个受控补丁绕过 Planner 后修改父仓或子仓。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture, patch_content = self._delta_fixture(Path(temp_dir))
            self._advance_parent_only(fixture)
            self._apply_patch(fixture, patch_content)
            git = Git()
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)
            declarations = (
                ManagedPatch(
                    "first", "plugins/iceberg_delta", ".", patch_content
                ),
                ManagedPatch(
                    "second", "plugins/iceberg_delta", ".", patch_content
                ),
            )
            adapter._patch_loader = lambda commit: declarations
            before = self._domain_snapshot(fixture)
            before_files = self._worktree_file_bytes(fixture.submodule)

            facts = adapter.collect_plan_facts(git, fresh=True)
            planned = plan_sync(facts)
            result = execute_sync(git, adapter, None, True)

            self.assertEqual(facts.repositories[0].managed_patch_state, "transition")
            self.assertTrue(facts.managed_patch_transition)
            self.assertEqual(planned.state, "blocked")
            self.assertIn("managed_patch_transition_required", planned.reason_codes)
            self.assertEqual(
                (result.state, result.reason_codes),
                ("blocked", ("managed_patch_transition_required",)),
            )
            self.assertEqual(cli._exit_code(result), 2)
            self.assertEqual(self._domain_snapshot(fixture), before)
            self.assertEqual(self._worktree_file_bytes(fixture.submodule), before_files)

    def test_unchanged_declared_patch_absent_from_clean_worktree_blocks(self):
        """防止同步主动把未应用的受控补丁引入干净工作树。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture, _ = self._delta_fixture(Path(temp_dir))
            publisher = fixture.clone_parent("publisher")
            fixture.commit_file(publisher, "target.txt", "advance\n", "advance parent")
            fixture.push(publisher)
            git = Git()

            facts = DataInfraAdapter.for_workspace(
                config_for(fixture), git
            ).collect_plan_facts(git, fresh=True)
            result = plan_sync(facts)

            self.assertEqual(facts.repositories[0].facts.worktree, "clean")
            self.assertEqual(facts.repositories[0].managed_patch_state, "transition")
            self.assertEqual(result.state, "blocked")
            self.assertIn("managed_patch_transition_required", result.reason_codes)

            before = (
                fixture.rev_parse(fixture.parent, "HEAD"),
                fixture.rev_parse(fixture.submodule, "HEAD"),
                fixture._run(
                    fixture.submodule,
                    ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                ).stdout,
            )
            applied = execute_sync(
                git,
                DataInfraAdapter.for_workspace(config_for(fixture), git),
                None,
                True,
            )
            after = (
                fixture.rev_parse(fixture.parent, "HEAD"),
                fixture.rev_parse(fixture.submodule, "HEAD"),
                fixture._run(
                    fixture.submodule,
                    ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                ).stdout,
            )
            self.assertEqual(applied.state, "blocked")
            self.assertIn("managed_patch_transition_required", applied.reason_codes)
            self.assertFalse(applied.changed)
            self.assertEqual(after, before)

    def test_target_patch_preflight_never_registers_a_real_worktree(self):
        """防止临时验证清理失败在真实子仓留下 worktree metadata。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture, patch_content = self._delta_fixture(Path(temp_dir))
            self._apply_patch(fixture, patch_content)
            delegate = Git()

            class RejectWorktreeRemovalGit:
                def __getattr__(self, name):
                    return getattr(delegate, name)

                def run(self, repo, args, *, check=True):
                    if args[:2] == ("worktree", "remove"):
                        raise OSError("injected worktree cleanup failure")
                    return delegate.run(repo, args, check=check)

            git = RejectWorktreeRemovalGit()
            before = delegate.run(
                fixture.submodule, ("worktree", "list", "--porcelain")
            ).stdout
            adapter = DataInfraAdapter.for_workspace(config_for(fixture), git)

            facts = adapter.collect_plan_facts(git, fresh=False)

            after = delegate.run(
                fixture.submodule, ("worktree", "list", "--porcelain")
            ).stdout
            self.assertEqual(facts.repositories[0].managed_patch_state, "continuous")
            self.assertEqual(after, before)

    def test_changed_patch_or_extra_dirty_is_transition(self):
        """防止声明变化或额外 dirty 获得连续补丁例外。"""
        for scenario in ("changed", "extra-dirty"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                fixture, patch_content = self._delta_fixture(Path(temp_dir))
                self._apply_patch(fixture, patch_content)
                if scenario == "changed":
                    publisher = fixture.clone_parent("publisher")
                    (publisher / "build/patches/iceberg-delta-cmake-pie-filter.patch").write_bytes(
                        patch_content + b"\n# declaration changed\n"
                    )
                    fixture._run(publisher, ("add", "build/patches"))
                    fixture._run(publisher, ("commit", "-m", "change patch declaration"))
                    fixture.push(publisher)
                else:
                    fixture.make_dirty(fixture.submodule, "extra.txt")
                git = Git()

                facts = DataInfraAdapter.for_workspace(
                    config_for(fixture), git
                ).collect_plan_facts(git, fresh=scenario == "changed")

                self.assertEqual(
                    facts.repositories[0].managed_patch_state, "transition"
                )

    def test_unchanged_patch_unapplicable_to_target_is_transition(self):
        """防止目标 pin 无法接纳补丁时继续自动重放。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture, patch_content = self._delta_fixture(Path(temp_dir))
            self._apply_patch(fixture, patch_content)
            publisher = fixture.clone_parent("publisher")
            fixture._run(
                publisher,
                ("-c", "protocol.file.allow=always", "submodule", "update", "--init"),
            )
            child = publisher / "plugins/iceberg_delta"
            fixture._configure_user(child)
            fixture.commit_file(child, "README.md", "conflicting target\n", "conflict patch")
            fixture._run(child, ("push", "origin", "HEAD:main"))
            fixture._run(publisher, ("add", "plugins/iceberg_delta"))
            fixture._run(publisher, ("commit", "-m", "advance conflicting target"))
            fixture.push(publisher)
            git = Git()

            facts = DataInfraAdapter.for_workspace(
                config_for(fixture), git
            ).collect_plan_facts(git, fresh=True)

            self.assertEqual(facts.repositories[0].managed_patch_state, "transition")

    @staticmethod
    def _delta_fixture(root):
        fixture = CompositeFixture.create(root)
        (fixture.parent / "plugins").mkdir()
        fixture._run(fixture.parent, ("mv", "modules/component", "plugins/iceberg_delta"))
        fixture.submodule = fixture.parent / "plugins/iceberg_delta"
        fixture.write_file(fixture.submodule, "README.md", "patched submodule\n")
        patch_content = fixture._run(
            fixture.submodule, ("diff", "--", "README.md")
        ).stdout.encode("utf-8")
        fixture._run(fixture.submodule, ("checkout", "--", "README.md"))
        patch_path = fixture.parent / "build/patches/iceberg-delta-cmake-pie-filter.patch"
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_bytes(patch_content)
        fixture._run(
            fixture.parent,
            ("add", ".gitmodules", "plugins/iceberg_delta", "build/patches"),
        )
        fixture._run(fixture.parent, ("commit", "-m", "declare delta patch"))
        fixture.push(fixture.parent)
        fixture.target_parent = fixture.rev_parse(fixture.parent, "HEAD")
        return fixture, patch_content

    @staticmethod
    def _apply_patch(fixture, patch_content):
        patch_file = fixture.root / "managed.patch"
        patch_file.write_bytes(patch_content)
        fixture._run(fixture.submodule, ("apply", str(patch_file)))

    @staticmethod
    def _advance_parent_only(fixture):
        publisher = fixture.clone_parent("publisher")
        fixture.commit_file(publisher, "target.txt", "advance\n", "advance parent")
        fixture.push(publisher)
        return (
            fixture.rev_parse(publisher, "HEAD"),
            fixture.rev_parse(fixture.submodule, "HEAD"),
        )

    @staticmethod
    def _advance_target_with_patch(fixture):
        publisher = fixture.clone_parent("publisher")
        fixture._run(
            publisher,
            (
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--init",
                "--checkout",
            ),
        )
        child = publisher / "plugins/iceberg_delta"
        fixture._configure_user(child)
        fixture.commit_file(
            child, "README.md", "patched submodule\n", "include patch content"
        )
        fixture._run(child, ("push", "origin", "HEAD:main"))
        fixture._run(publisher, ("add", "plugins/iceberg_delta"))
        fixture._run(publisher, ("commit", "-m", "advance integrated patch pin"))
        fixture.push(publisher)
        return fixture.rev_parse(publisher, "HEAD"), fixture.rev_parse(child, "HEAD")

    @staticmethod
    def _advance_target_without_patch(fixture):
        publisher = fixture.clone_parent("publisher")
        fixture._run(
            publisher,
            (
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--init",
                "--checkout",
            ),
        )
        child = publisher / "plugins/iceberg_delta"
        fixture._configure_user(child)
        fixture.commit_file(child, "target.txt", "target\n", "advance target")
        fixture._run(child, ("push", "origin", "HEAD:main"))
        fixture._run(publisher, ("add", "plugins/iceberg_delta"))
        fixture._run(publisher, ("commit", "-m", "advance target pin"))
        fixture.push(publisher)
        return fixture.rev_parse(publisher, "HEAD"), fixture.rev_parse(child, "HEAD")

    @staticmethod
    def _default_cli_environment(fixture):
        environment = dict(os.environ)
        environment.update(
            {
                "DATA_INFRA_SYNC_ROOT": str(fixture.parent),
                "XDG_CONFIG_HOME": str(fixture.root / "config-home"),
                "XDG_STATE_HOME": str(fixture.root / "state-home"),
            }
        )
        config = load_config({}, environment, None)
        write_config(config)
        return environment, config

    @staticmethod
    def _domain_snapshot(fixture):
        return (
            fixture.rev_parse(fixture.parent, "HEAD"),
            fixture.rev_parse(fixture.submodule, "HEAD"),
            fixture._run(
                fixture.submodule,
                ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ).stdout,
        )

    @staticmethod
    def _worktree_file_bytes(root):
        """返回工作树全部常规文件的相对路径和原始字节，排除 Git 元数据。"""
        return tuple(
            (path.relative_to(root).as_posix(), path.read_bytes())
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.relative_to(root).parts
        )

if __name__ == "__main__":
    unittest.main()

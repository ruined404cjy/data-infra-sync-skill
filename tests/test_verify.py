import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.adapters.datainfra import DataInfraInstallAdapter
from data_infra_sync.state import StateStore
from data_infra_sync.verify import InstallIdentity, collect_install_identity, verify_install


def _git(repo, *args):
    return subprocess.run(
        ("git",) + args,
        cwd=str(repo),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _init_repository(path, content):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "Fixture User")
    _git(path, "config", "user.email", "fixture@example.invalid")
    (path / "tracked.txt").write_text(content, encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "fixture")
    return _git(path, "rev-parse", "HEAD")


def _make_workspace(root):
    child = root / "modules/child"
    child_head = _init_repository(child, "child\n")
    parent_head = _init_repository(root, "parent\n")
    _git(root, "update-index", "--add", "--cacheinfo", "160000," + child_head + ",modules/child")
    _git(root, "commit", "-m", "record child")
    parent_head = _git(root, "rev-parse", "HEAD")
    adapter = DataInfraInstallAdapter(root, proc_reader=lambda: ())
    for index, (_, paths) in enumerate(adapter.artifact_groups()):
        content = ("group-{}\n".format(index)).encode("utf-8")
        for relative in paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    return adapter, parent_head, child_head


def _config(root, state_dir):
    return SimpleNamespace(root=root, state_dir=state_dir)


class InstallIdentityTest(unittest.TestCase):
    def test_install_identity_is_immutable_and_manifest_is_root_independent(self):
        """防止安装身份携带绝对 checkout 路径或可被调用方修改。"""
        identity = InstallIdentity(
            repositories=((".", "a" * 40),),
            artifacts=(("build/library.so", "b" * 64),),
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            identity.repositories = ()
        self.assertEqual(
            identity.to_manifest(),
            {
                "format": "1",
                "repositories": {".": "a" * 40},
                "artifacts": {"build/library.so": "b" * 64},
            },
        )

    def test_default_adapter_declares_all_native_artifact_groups(self):
        """防止默认安装布局漏检构建、依赖或安装副本。"""
        adapter = DataInfraInstallAdapter(Path("/checkout"), proc_reader=lambda: ())

        self.assertEqual(
            adapter.artifact_groups(),
            (
                ("bridge", (
                    "deps/iceberg-rust-bridge/target/release/libiceberg_rust_bridge.so",
                    "plugins/openGauss-Catalog/deps/libiceberg_rust_bridge.so",
                    "mppdb_temp_install/lib/postgresql/libiceberg_rust_bridge.so",
                )),
                ("catalog", (
                    "plugins/openGauss-Catalog/iceberg_catalog.so",
                    "mppdb_temp_install/lib/postgresql/iceberg_catalog.so",
                    "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_catalog.so",
                )),
                ("fdw", (
                    "plugins/iceberg_fdw/iceberg_fdw.so",
                    "mppdb_temp_install/lib/postgresql/iceberg_fdw.so",
                    "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_fdw.so",
                )),
                ("delta", (
                    "plugins/iceberg_delta/tmp_build_gcc10/iceberg_delta.so",
                    "mppdb_temp_install/lib/postgresql/iceberg_delta.so",
                    "mppdb_temp_install/lib/postgresql/proc_srclib/iceberg_delta.so",
                )),
                ("catalog-control", (
                    "plugins/openGauss-Catalog/iceberg_catalog.control",
                    "mppdb_temp_install/share/postgresql/extension/iceberg_catalog.control",
                )),
                ("catalog-sql", (
                    "plugins/openGauss-Catalog/iceberg_catalog--1.0.0.sql",
                    "mppdb_temp_install/share/postgresql/extension/iceberg_catalog--1.0.0.sql",
                )),
                ("fdw-control", (
                    "plugins/iceberg_fdw/iceberg_fdw.control",
                    "mppdb_temp_install/share/postgresql/extension/iceberg_fdw.control",
                )),
                ("fdw-sql", (
                    "plugins/iceberg_fdw/iceberg_fdw--0.1.0.sql",
                    "mppdb_temp_install/share/postgresql/extension/iceberg_fdw--0.1.0.sql",
                )),
                ("delta-control", (
                    "plugins/iceberg_delta/iceberg_delta.control",
                    "mppdb_temp_install/share/postgresql/extension/iceberg_delta.control",
                )),
                ("delta-sql", (
                    "plugins/iceberg_delta/iceberg_delta--1.0.0.sql",
                    "mppdb_temp_install/share/postgresql/extension/iceberg_delta--1.0.0.sql",
                )),
            ),
        )

    def test_collect_records_parent_submodule_heads_and_every_relative_artifact(self):
        """防止身份遗漏实际一级 submodule HEAD 或写入绝对路径。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            adapter, parent_head, child_head = _make_workspace(root)
            config = _config(root, Path(directory) / "state")

            identity = collect_install_identity(config, adapter)

            self.assertEqual(dict(identity.repositories), {".": parent_head, "modules/child": child_head})
            self.assertEqual(len(identity.artifacts), 24)
            self.assertTrue(all(not Path(path).is_absolute() for path, _ in identity.artifacts))
            self.assertEqual(
                dict(identity.artifacts)["plugins/iceberg_fdw/iceberg_fdw.so"],
                hashlib.sha256(b"group-2\n").hexdigest(),
            )

    def test_collect_uses_the_injected_filesystem_reader(self):
        """防止测试和调用方必须访问真实 DataInfra 文件系统。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            adapter, _, _ = _make_workspace(root)
            reads = []

            def read_file(workspace, relative):
                reads.append(relative)
                return (workspace / relative).read_bytes()

            adapter.file_reader = read_file
            collect_install_identity(_config(root, Path(directory) / "state"), adapter)

            self.assertEqual(len(reads), 24)
            self.assertEqual(set(reads), {path for _, paths in adapter.artifact_groups() for path in paths})

    def test_identical_checkouts_at_different_absolute_roots_have_same_manifest(self):
        """防止绝对 workspace 路径使相同安装身份产生不同 manifest。"""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first_adapter, _, _ = _make_workspace(base / "first")
            second_adapter, _, _ = _make_workspace(base / "second")
            first = collect_install_identity(_config(base / "first", base / "state-a"), first_adapter)
            second = collect_install_identity(_config(base / "second", base / "state-b"), second_adapter)

            self.assertEqual(first.to_manifest(), second.to_manifest())


class VerifyInstallTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "checkout"
        self.adapter, _, _ = _make_workspace(self.root)
        self.config = _config(self.root, self.base / "state")
        self.store = StateStore(self.config.state_dir)
        self.adapter_patch = mock.patch(
            "data_infra_sync.verify.DataInfraInstallAdapter", return_value=self.adapter
        )
        self.adapter_patch.start()

    def tearDown(self):
        self.adapter_patch.stop()
        self.temporary.cleanup()

    def test_missing_manifest_requires_build_with_only_record_next_action(self):
        """防止首次核验误报部署一致或暴露不存在的 build 子命令。"""
        result = verify_install(self.config, self.store, record=False)

        self.assertEqual(result.state, "build_required")
        self.assertEqual(result.reason_codes, ("manifest_missing",))
        self.assertEqual(result.next_actions[0].kind, "verify_install_record")
        self.assertEqual(result.next_actions[0].argv, ("data-infra-sync", "verify", "install", "--record"))
        self.assertEqual(result.next_actions[0].preconditions, ("build_completed",))

    def test_record_writes_current_identity_and_normal_mode_matches_it(self):
        """防止 record 写入运行时数据或普通模式跳过旧 manifest。"""
        recorded = verify_install(self.config, self.store, record=True)
        manifest = json.loads((self.config.state_dir / "manifest.json").read_text())
        checked = verify_install(self.config, self.store, record=False)

        self.assertEqual(recorded.state, "deployment_consistent")
        self.assertTrue(recorded.changed)
        self.assertEqual(checked.state, "deployment_consistent")
        self.assertFalse(checked.changed)
        self.assertEqual(set(manifest), {"format", "repositories", "artifacts"})
        self.assertNotIn(str(self.root), json.dumps(manifest))

    def test_source_change_requires_build_but_artifact_change_is_deployment_mismatch(self):
        """防止源码过期和部署副本偏离被归入同一状态。"""
        verify_install(self.config, self.store, record=True)
        old_manifest = (self.config.state_dir / "manifest.json").read_bytes()
        (self.root / "tracked.txt").write_text("parent changed\n", encoding="utf-8")
        _git(self.root, "add", "tracked.txt")
        _git(self.root, "commit", "-m", "source changed")

        source_result = verify_install(self.config, self.store, record=False)

        self.assertEqual(source_result.state, "build_required")
        self.assertIn("source_identity_changed", source_result.reason_codes)
        self.assertEqual((self.config.state_dir / "manifest.json").read_bytes(), old_manifest)

        # 恢复源码身份后整体替换一个产物组，保持组内一致但偏离 manifest。
        _git(self.root, "reset", "--hard", json.loads(old_manifest)["repositories"]["."])
        for relative in self.adapter.artifact_groups()[1][1]:
            (self.root / relative).write_bytes(b"new catalog\n")
        artifact_result = verify_install(self.config, self.store, record=False)
        self.assertEqual(artifact_result.state, "deployment_mismatch")
        self.assertEqual(artifact_result.reason_codes, ("artifact_manifest_mismatch",))

    def test_missing_or_inconsistent_artifacts_are_deployment_mismatch(self):
        """防止缺失文件或组内不同 SHA 被 record 接受。"""
        missing = self.root / self.adapter.artifact_groups()[0][1][0]
        missing.unlink()
        missing_result = verify_install(self.config, self.store, record=True)
        self.assertEqual(missing_result.state, "deployment_mismatch")
        self.assertEqual(missing_result.reason_codes, ("artifact_missing",))

        missing.write_bytes(b"group-0\n")
        (self.root / self.adapter.artifact_groups()[2][1][1]).write_bytes(b"different\n")
        mismatch_result = verify_install(self.config, self.store, record=True)
        self.assertEqual(mismatch_result.state, "deployment_mismatch")
        self.assertEqual(mismatch_result.reason_codes, ("artifact_group_mismatch",))

    def test_record_skips_old_manifest_and_failure_preserves_its_exact_bytes(self):
        """防止旧身份阻止合法覆盖，或失败 record 破坏上次可用 manifest。"""
        verify_install(self.config, self.store, record=True)
        path = self.config.state_dir / "manifest.json"
        old = path.read_bytes()
        for relative in self.adapter.artifact_groups()[0][1]:
            (self.root / relative).write_bytes(b"replacement\n")
        replaced = verify_install(self.config, self.store, record=True)
        self.assertEqual(replaced.state, "deployment_consistent")
        self.assertNotEqual(path.read_bytes(), old)

        stable = path.read_bytes()
        (self.root / self.adapter.artifact_groups()[3][1][0]).unlink()
        failed = verify_install(self.config, self.store, record=True)
        self.assertEqual(failed.state, "deployment_mismatch")
        self.assertEqual(path.read_bytes(), stable)

    def test_deleted_library_and_other_workspace_mappings_are_mismatch_but_sysv_is_normal(self):
        """防止 gaussdb 保留已删除库或从其他 checkout 加载关键库。"""
        good_exe = str(self.root / "mppdb_temp_install/bin/gaussdb")
        cases = (
            (({"name": "gaussdb", "exe": good_exe, "maps": ("/tmp/libother.so (deleted)",)},), "deleted_library_mapping"),
            (({"name": "gaussdb", "exe": good_exe, "maps": ("/SYSV00000000 (deleted)",)},), None),
            (({"name": "gaussdb", "exe": good_exe, "maps": ("/other/workspace/iceberg_fdw.so",)},), "other_workspace_mapping"),
            (({"name": "gaussdb", "exe": "/other/workspace/gaussdb", "maps": ()},), "other_workspace_mapping"),
            (({"name": "postgres", "exe": "/other/workspace/gaussdb", "maps": ("/tmp/a.so (deleted)",)},), None),
        )
        for processes, expected in cases:
            with self.subTest(expected=expected, processes=processes):
                self.adapter.proc_reader = lambda processes=processes: processes
                result = verify_install(self.config, self.store, record=True)
                if expected is None:
                    self.assertEqual(result.state, "deployment_consistent")
                else:
                    self.assertEqual(result.state, "deployment_mismatch")
                    self.assertEqual(result.reason_codes, (expected,))

    def test_git_file_proc_and_manifest_read_failures_are_failed(self):
        """防止无法完成读取时降级为可由构建修复的状态。"""
        verify_install(self.config, self.store, record=True)
        manifest = self.config.state_dir / "manifest.json"
        manifest.write_text("not-json\n", encoding="utf-8")
        self.assertEqual(verify_install(self.config, self.store, record=False).state, "failed")

        self.adapter.proc_reader = lambda: (_ for _ in ()).throw(OSError("proc denied"))
        self.assertEqual(verify_install(self.config, self.store, record=True).state, "failed")

    def test_artifact_symlink_cannot_escape_workspace(self):
        """防止 hash 读取跟随产物 symlink 访问 workspace 外文件。"""
        target = self.base / "outside.so"
        target.write_bytes(b"group-0\n")
        artifact = self.root / self.adapter.artifact_groups()[0][1][0]
        artifact.unlink()
        os.symlink(target, artifact)

        result = verify_install(self.config, self.store, record=True)

        self.assertEqual(result.state, "deployment_mismatch")
        self.assertEqual(result.reason_codes, ("artifact_not_regular",))

if __name__ == "__main__":
    unittest.main()

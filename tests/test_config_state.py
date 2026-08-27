import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.config import WorkspaceConfig, load_config, write_config
from data_infra_sync.model import Result
from data_infra_sync.state import StateStore


class WorkspaceConfigTest(unittest.TestCase):
    def test_load_config_accepts_supported_target_names(self):
        """防止目标名称约束拒绝 Git 支持的常用远端或分支名。"""
        cases = (
            {"target_remote": "origin", "target_branch": "main"},
            {"target_remote": "upstream-2", "target_branch": "release/1.0"},
        )

        for values in cases:
            with self.subTest(values=values):
                config = load_config(values, {}, None)

                self.assertEqual(config.target_remote, values["target_remote"])
                self.assertEqual(config.target_branch, values["target_branch"])

    def test_load_config_rejects_unsupported_target_names(self):
        """防止 URL、路径和凭据文本成为目标身份。"""
        invalid = (
            {"target_remote": "https://user:secret@example.invalid/repo"},
            {"target_remote": "team/origin"},
            {"target_branch": "feature/token=value"},
            {"target_branch": "feature/@secret"},
            {"target_branch": "-leading-dash"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                load_config(values, {}, None)

    def test_load_config_checks_git_branch_format_before_segment_format(self):
        """防止分段规则在 Git 分支语义校验前提前拒绝输入。"""
        completed = subprocess.CompletedProcess((), 0)
        with patch(
            "data_infra_sync.config.subprocess.run", return_value=completed
        ) as run:
            with self.assertRaisesRegex(ValueError, "unsupported target branch"):
                load_config({"target_branch": "feature/token=value"}, {}, None)

        run.assert_called_once_with(
            ["git", "check-ref-format", "--branch", "feature/token=value"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_write_config_round_trips_all_canonical_values(self):
        """防止 init 写出的配置无法由公开读取入口恢复。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config = WorkspaceConfig(
                (temp_path / "parent checkout").resolve(),
                "upstream",
                "release/next",
                temp_path / "nested/workspace.conf",
                (temp_path / "state directory").resolve(),
            )

            write_config(config)

            persisted = config.config_path.read_text(encoding="utf-8")
            self.assertIn("targetRemote", persisted)
            self.assertIn("targetBranch", persisted)
            self.assertIn("stateDir", persisted)
            self.assertEqual(load_config({}, {}, config.config_path), config)

    def test_write_config_failure_preserves_old_bytes_and_removes_temporary_file(self):
        """防止替换前失败破坏现有配置或遗留临时文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "workspace.conf"
            old_bytes = b"[existing]\n\tvalue = preserved\n"
            config_path.write_bytes(old_bytes)
            config = WorkspaceConfig(
                temp_path / "parent",
                "origin",
                "main",
                config_path,
                temp_path / "state",
            )

            with patch("data_infra_sync.config.os.replace", side_effect=OSError("injected")):
                with self.assertRaisesRegex(OSError, "injected"):
                    write_config(config)

            self.assertEqual(config_path.read_bytes(), old_bytes)
            self.assertEqual(list(temp_path.glob(".*.tmp")), [])

    def test_write_config_rejects_values_that_can_inject_git_config_records(self):
        """防止换行和 NUL 将单个配置值扩展为额外键。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            baseline = WorkspaceConfig(
                temp_path / "parent",
                "origin",
                "main",
                temp_path / "workspace.conf",
                temp_path / "state",
            )
            cases = (
                WorkspaceConfig(
                    baseline.root,
                    "origin\nmalicious.key=value",
                    baseline.target_branch,
                    baseline.config_path,
                    baseline.state_dir,
                ),
                WorkspaceConfig(
                    baseline.root,
                    baseline.target_remote,
                    "main\0malicious",
                    baseline.config_path,
                    baseline.state_dir,
                ),
            )

            for config in cases:
                with self.subTest(config=config):
                    with self.assertRaises(ValueError):
                        write_config(config)
                    self.assertFalse(config.config_path.exists())
    def test_adapter_defaults_supply_current_root_origin_and_main(self):
        """防止缺少全部配置时偏离适配器的最小默认值。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            previous_cwd = Path.cwd()
            try:
                os.chdir(temp_path)
                config = load_config({}, {}, temp_path / "missing.conf")
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(config.root, temp_path.resolve())
            self.assertEqual(config.target_remote, "origin")
            self.assertEqual(config.target_branch, "main")

    def test_cli_values_override_environment_file_and_defaults(self):
        """防止较低优先级配置覆盖显式命令行参数。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "workspace.conf"
            self._write_config(
                config_path,
                {
                    "root": str(temp_path / "file-root"),
                    "targetRemote": "file-remote",
                    "targetBranch": "file-branch",
                    "stateDir": str(temp_path / "file-state"),
                },
            )

            config = load_config(
                {
                    "root": str(temp_path / "cli-root"),
                    "target_remote": "cli-remote",
                    "target_branch": "cli-branch",
                    "state_dir": str(temp_path / "cli-state"),
                },
                {
                    "DATA_INFRA_SYNC_ROOT": str(temp_path / "env-root"),
                    "DATA_INFRA_SYNC_TARGET_REMOTE": "env-remote",
                    "DATA_INFRA_SYNC_TARGET_BRANCH": "env-branch",
                    "DATA_INFRA_SYNC_STATE_DIR": str(temp_path / "env-state"),
                },
                config_path,
            )

            self.assertEqual(config.root, (temp_path / "cli-root").resolve())
            self.assertEqual(config.target_remote, "cli-remote")
            self.assertEqual(config.target_branch, "cli-branch")
            self.assertEqual(config.state_dir, (temp_path / "cli-state").resolve())
            self.assertEqual(config.config_path, config_path.resolve())

    def test_environment_values_override_file_and_defaults(self):
        """防止工作区配置覆盖调用环境。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "workspace.conf"
            self._write_config(
                config_path,
                {"targetRemote": "file-remote", "targetBranch": "file-branch"},
            )

            config = load_config(
                {},
                {
                    "DATA_INFRA_SYNC_TARGET_REMOTE": "env-remote",
                    "DATA_INFRA_SYNC_TARGET_BRANCH": "env-branch",
                },
                config_path,
            )

            self.assertEqual(config.target_remote, "env-remote")
            self.assertEqual(config.target_branch, "env-branch")

    def test_workspace_file_values_override_adapter_defaults(self):
        """防止适配器默认值覆盖已保存的工作区选择。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / "workspace.conf"
            self._write_config(
                config_path,
                {"targetRemote": "file-remote", "targetBranch": "file-branch"},
            )

            config = load_config({}, {}, config_path)

            self.assertEqual(config.target_remote, "file-remote")
            self.assertEqual(config.target_branch, "file-branch")

    def test_default_config_and_state_paths_use_distinct_canonical_workspace_keys(self):
        """防止不同绝对工作区共享配置或状态目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            environ = {
                "XDG_CONFIG_HOME": str(temp_path / "config"),
                "XDG_STATE_HOME": str(temp_path / "state"),
            }

            first = load_config({"root": str(temp_path / "first")}, environ, None)
            second = load_config({"root": str(temp_path / "second")}, environ, None)

            first_key = hashlib.sha256(str((temp_path / "first").resolve()).encode()).hexdigest()[:16]
            second_key = hashlib.sha256(str((temp_path / "second").resolve()).encode()).hexdigest()[:16]
            self.assertNotEqual(first_key, second_key)
            self.assertEqual(first.config_path, temp_path / "config/data-infra-sync-skill" / (first_key + ".conf"))
            self.assertEqual(second.config_path, temp_path / "config/data-infra-sync-skill" / (second_key + ".conf"))
            self.assertEqual(first.state_dir, temp_path / "state/data-infra-sync-skill" / first_key)
            self.assertEqual(second.state_dir, temp_path / "state/data-infra-sync-skill" / second_key)

    def test_workspace_root_from_default_file_changes_state_key_not_config_path(self):
        """防止文件中的 root 仍使用查找配置时的旧状态目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            initial_root = temp_path / "initial-root"
            final_root = temp_path / "final-root"
            environ = {
                "XDG_CONFIG_HOME": str(temp_path / "config"),
                "XDG_STATE_HOME": str(temp_path / "state"),
            }
            initial_key = hashlib.sha256(str(initial_root.resolve()).encode()).hexdigest()[:16]
            final_key = hashlib.sha256(str(final_root.resolve()).encode()).hexdigest()[:16]
            config_path = temp_path / "config/data-infra-sync-skill" / (initial_key + ".conf")
            self._write_config(config_path, {"root": str(final_root)})

            initial_root.mkdir()
            previous_cwd = Path.cwd()
            try:
                os.chdir(initial_root)
                config = load_config({}, environ, None)
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(config.config_path, config_path)
            self.assertEqual(config.root, final_root.resolve())
            self.assertEqual(config.state_dir, temp_path / "state/data-infra-sync-skill" / final_key)

    @staticmethod
    def _write_config(path, values):
        path.parent.mkdir(parents=True, exist_ok=True)
        for key, value in values.items():
            subprocess.run(
                ["git", "config", "--file", str(path), "data-infra-sync." + key, value],
                check=True,
            )


class StateStoreTest(unittest.TestCase):
    def test_dynamic_gitlink_key_does_not_redact_oid(self):
        """防止路径名中的 key 误触发字段名脱敏。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = StateStore(root)
            oid = "a" * 40
            result = Result(
                "inspect",
                "up_to_date",
                (),
                {"parent_commit": "b" * 40, "gitlinks": {"modules/monkey": oid}},
                (),
                False,
                (),
                None,
                False,
            )

            store.write_latest(result)
            store.append_event(result)

            latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            event = json.loads((root / "events.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(latest["target"]["gitlinks"]["modules/monkey"], oid)
            self.assertEqual(event["target"]["gitlinks"]["modules/monkey"], oid)

    def test_latest_replaces_atomically_without_temporary_files(self):
        """防止状态写入留下可被后续读取器误判的临时 JSON。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))
            result = Result("inspect", "up_to_date", (), None, (), False, (), None, False)

            store.write_latest(result)

            self.assertEqual(json.loads((Path(temp_dir) / "latest.json").read_text())["state"], "up_to_date")
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_second_lock_attempt_fails_without_waiting(self):
        """防止两个同步进程同时修改同一状态目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))

            with store.lock():
                with self.assertRaises(BlockingIOError):
                    with StateStore(Path(temp_dir)).lock():
                        pass

    def test_persisted_json_redacts_url_userinfo_tokens_and_environment_values(self):
        """防止凭据通过结果、manifest 或环境变量进入磁盘。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))
            secret = "environment-secret-value"
            inline_token = "inline-token-value"
            result = Result(
                "inspect",
                "blocked",
                ("token=" + secret,),
                {
                    "remote_url": "https://user:token@example.invalid/repository?token=" + inline_token,
                    "token": inline_token,
                },
                (),
                False,
                (),
                None,
                False,
            )

            with patch.dict(os.environ, {"DATA_INFRA_SYNC_TOKEN": secret}, clear=False):
                store.write_latest(result)
                store.append_event(result)
                store.write_manifest({"source": secret, "url": "ssh://user:token@example.invalid/repository"})

            persisted = "\n".join(
                path.read_text() for path in Path(temp_dir).glob("*.json*")
            )
            self.assertNotIn("user:token", persisted)
            self.assertNotIn("token@example.invalid", persisted)
            self.assertNotIn(secret, persisted)
            self.assertNotIn(inline_token, persisted)
            self.assertEqual(json.loads((Path(temp_dir) / "latest.json").read_text())["schema_version"], "1")

    def test_short_sensitive_environment_value_is_redacted(self):
        """防止短 token 值绕过按长度筛选的脱敏。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))
            short_secret = "a9!"
            result = Result(
                "inspect",
                "blocked",
                (),
                {"diagnostic": short_secret},
                (),
                False,
                (),
                None,
                False,
            )

            with patch.dict(os.environ, {"SERVICE_TOKEN": short_secret}, clear=False):
                store.write_latest(result)

            latest = json.loads((Path(temp_dir) / "latest.json").read_text())
            self.assertNotIn(short_secret, json.dumps(latest))
            self.assertEqual(latest["schema_version"], "1")

    def test_private_token_query_is_redacted(self):
        """防止 private_token 查询参数绕过 URL 脱敏。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))
            query_secret = "query-secret-19d4"
            result = Result(
                "inspect",
                "blocked",
                (),
                {"remote_url": "https://example.invalid/repository?private_token=" + query_secret},
                (),
                False,
                (),
                None,
                False,
            )

            store.write_latest(result)

            latest = json.loads((Path(temp_dir) / "latest.json").read_text())
            self.assertNotIn(query_secret, json.dumps(latest))
            self.assertEqual(latest["schema_version"], "1")

    def test_password_assignment_text_is_redacted(self):
        """防止非 URL 文本中的 password 赋值进入持久化状态。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))
            helper_secret = "helper-secret-72b1"
            result = Result(
                "inspect",
                "blocked",
                ("password=" + helper_secret,),
                {"diagnostic": "password:" + helper_secret},
                (),
                False,
                (),
                None,
                False,
            )

            store.write_latest(result)

            latest = json.loads((Path(temp_dir) / "latest.json").read_text())
            self.assertNotIn(helper_secret, json.dumps(latest))
            self.assertEqual(latest["schema_version"], "1")

    def test_schema_version_skips_short_environment_value_replacement(self):
        """防止敏感环境变量值与 schema 常量相同导致协议失效。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))
            result = Result(
                "inspect",
                "blocked",
                (),
                {"diagnostic": "1"},
                (),
                False,
                (),
                None,
                False,
            )

            with patch.dict(os.environ, {"SERVICE_TOKEN": "1"}, clear=False):
                store.write_latest(result)

            latest = json.loads((Path(temp_dir) / "latest.json").read_text())
            self.assertEqual(latest["schema_version"], "1")
            self.assertEqual(latest["target"]["diagnostic"], "[REDACTED]")

    def test_state_skips_environment_value_replacement_while_free_text_is_redacted(self):
        """防止敏感环境变量值与 state 枚举相同导致状态协议失效。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))
            result = Result(
                "inspect",
                "blocked",
                (),
                {"diagnostic": "blocked"},
                (),
                False,
                (),
                None,
                False,
            )

            with patch.dict(os.environ, {"SERVICE_TOKEN": "blocked"}, clear=False):
                store.write_latest(result)

            latest = json.loads((Path(temp_dir) / "latest.json").read_text())
            self.assertEqual(latest["state"], "blocked")
            self.assertEqual(latest["target"]["diagnostic"], "[REDACTED]")

    def test_top_level_reason_codes_skip_environment_value_replacement(self):
        """防止敏感环境变量值改写顶层结构化原因码。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StateStore(Path(temp_dir))
            result = Result(
                "inspect",
                "blocked",
                ("blocked",),
                {"diagnostic": "blocked"},
                (),
                False,
                (),
                None,
                False,
            )

            with patch.dict(os.environ, {"SERVICE_TOKEN": "blocked"}, clear=False):
                store.write_latest(result)

            latest = json.loads((Path(temp_dir) / "latest.json").read_text())
            self.assertEqual(latest["reason_codes"], ["blocked"])
            self.assertEqual(latest["target"]["diagnostic"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()

"""CLI 命令契约与编排测试。"""

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_infra_sync.config import WorkspaceConfig
from data_infra_sync.git import GitError
from data_infra_sync.model import Action, Result
from data_infra_sync import cli
from tests.test_executor import RecordingGit, ScriptedAdapter, TARGET_PARENT


OID = "1" * 40
SNAPSHOT = "a" * 64


def result(command="inspect", state="up_to_date", **changes):
    values = {
        "command": command,
        "state": state,
        "reason_codes": (),
        "target": None,
        "repositories": (),
        "changed": False,
        "next_actions": (),
        "snapshot": SNAPSHOT,
        "stale_target": False,
    }
    values.update(changes)
    return Result(**values)


class ParserTests(unittest.TestCase):
    def parse(self, *argv):
        return cli._build_parser().parse_args(argv)

    def test_help_lists_only_the_designed_root_commands(self):
        output = cli._build_parser().format_help()
        for command in ("init", "inspect", "branch", "sync", "verify"):
            self.assertIn(command, output)
        self.assertNotIn("build", output)

    def test_nested_help_lists_leaf_commands(self):
        for argv, expected in (
            (("branch", "--help"), ("status", "start", "resume", "publish-check")),
            (("sync", "--help"), ("plan", "apply")),
            (("verify", "--help"), ("install",)),
        ):
            stderr = io.StringIO()
            stdout = io.StringIO()
            with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.parse(*argv)
            self.assertEqual(raised.exception.code, 0)
            for command in expected:
                self.assertIn(command, stdout.getvalue())

    def test_common_options_work_before_root_command_or_after_leaf(self):
        before = self.parse(
            "--config", "cfg", "--root", "repo", "--target-remote", "mine",
            "--target-branch", "next", "--state-dir", "state", "--format", "json",
            "sync", "plan",
        )
        after = self.parse(
            "sync", "plan", "--config", "cfg", "--root", "repo",
            "--target-remote", "mine", "--target-branch", "next",
            "--state-dir", "state", "--format", "json",
        )
        for parsed in (before, after):
            self.assertEqual(parsed.config, "cfg")
            self.assertEqual(parsed.root, "repo")
            self.assertEqual(parsed.target_remote, "mine")
            self.assertEqual(parsed.target_branch, "next")
            self.assertEqual(parsed.state_dir, "state")
            self.assertEqual(parsed.output_format, "json")

    def test_leaf_defaults_do_not_override_root_common_options(self):
        parsed = self.parse("--format", "json", "--root", "repo", "inspect")
        self.assertEqual(parsed.output_format, "json")
        self.assertEqual(parsed.root, "repo")

    def test_branch_arguments_are_required_by_leaf(self):
        status = self.parse("branch", "status", "--repo", ".")
        start = self.parse("branch", "start", "--repo", "plugins/delta", "--name", "work")
        self.assertEqual(status.repo, ".")
        self.assertEqual((start.repo, start.name), ("plugins/delta", "work"))
        with self.assertRaises(SystemExit) as raised:
            self.parse("branch", "resume", "--repo", ".")
        self.assertEqual(raised.exception.code, 2)

    def test_apply_requires_exactly_one_valid_mode(self):
        self.assertEqual(self.parse("sync", "apply", "--snapshot", SNAPSHOT).snapshot, SNAPSHOT)
        self.assertTrue(self.parse("sync", "apply", "--non-interactive").non_interactive)
        for argv in (
            ("sync", "apply"),
            ("sync", "apply", "--snapshot", SNAPSHOT, "--non-interactive"),
            ("sync", "apply", "--snapshot", "A" * 64),
            ("sync", "apply", "--snapshot", "a" * 63),
        ):
            with self.assertRaises(SystemExit) as raised:
                self.parse(*argv)
            self.assertEqual(raised.exception.code, 2)

    def test_plan_offline_and_verify_record_are_optional_flags(self):
        self.assertTrue(self.parse("sync", "plan", "--offline").offline)
        self.assertTrue(self.parse("verify", "install", "--record").record)


class RenderingTests(unittest.TestCase):
    def test_json_is_one_result_object_and_exit_codes_are_stable(self):
        states = {
            "updated": 0,
            "blocked": 2,
            "waiting_for_pin": 2,
            "build_required": 2,
            "deployment_mismatch": 2,
            "publish_required": 2,
            "unconfigured": 2,
            "failed": 3,
            "partial": 4,
        }
        for state, expected in states.items():
            item = result(state=state)
            self.assertEqual(cli._exit_code(item), expected)
        self.assertEqual(cli._exit_code(result(state="unexpected")), 3)

        output = io.StringIO()
        item = result(reason_codes=("one",), stale_target=True)
        with contextlib.redirect_stdout(output):
            cli._render(item, "json")
        self.assertEqual(json.loads(output.getvalue()), item.to_dict())
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_text_contains_equivalent_control_fields_and_json_argv(self):
        action = Action("sync_apply", ("data-infra-sync", "sync", "apply", "x y"), True, False, ())
        item = result(reason_codes=("dirty_worktree",), next_actions=(action,), stale_target=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._render(item, "text")
        text = output.getvalue()
        for value in ("command: inspect", "state: up_to_date", "dirty_worktree", SNAPSHOT, "stale_target: true"):
            self.assertIn(value, text)
        self.assertIn('["data-infra-sync", "sync", "apply", "x y"]', text)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config_path = root / "workspace.conf"
        self.config_path.touch()
        self.config = WorkspaceConfig(root, "origin", "main", self.config_path, root / "state")
        self.store = Mock()
        self.store.lock.return_value = contextlib.nullcontext()
        self.adapter = Mock()
        self.facts = Mock()
        self.facts.parent.path = root
        self.facts.target_parent = OID
        self.facts.repositories = ()

    def tearDown(self):
        self.temporary.cleanup()

    def run_main(self, argv, service_result=None):
        service_result = service_result or result(command="sync plan")
        output = io.StringIO()
        patches = (
            patch.object(cli, "load_config", return_value=self.config),
            patch.object(cli, "StateStore", return_value=self.store),
            patch.object(cli, "Git"),
            patch.object(cli.DataInfraAdapter, "for_workspace", return_value=self.adapter),
            patch.object(cli, "plan_sync", return_value=service_result),
            patch.object(cli, "_render", wraps=cli._render),
        )
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(item) for item in patches]
            with contextlib.redirect_stdout(output):
                code = cli.main(argv)
        return code, output.getvalue(), mocks

    def test_inspect_collects_offline_plan_and_relabels_command(self):
        self.adapter.collect_plan_facts.return_value = self.facts
        code, output, mocks = self.run_main(("inspect", "--format", "json"))
        self.assertEqual(code, 0)
        self.adapter.collect_plan_facts.assert_called_once_with(mocks[2].return_value, fresh=False)
        mocks[4].assert_called_once_with(self.facts)
        self.assertEqual(json.loads(output)["command"], "inspect")

    def test_sync_plan_selects_fresh_by_default_and_offline_when_requested(self):
        self.adapter.collect_plan_facts.return_value = self.facts
        self.run_main(("sync", "plan"))
        self.adapter.collect_plan_facts.assert_called_once_with(unittest.mock.ANY, fresh=True)
        self.adapter.reset_mock()
        self.run_main(("sync", "plan", "--offline"))
        self.adapter.collect_plan_facts.assert_called_once_with(unittest.mock.ANY, fresh=False)

    def test_apply_calls_executor_with_exact_mode(self):
        with patch.object(cli, "execute_sync", return_value=result(command="sync apply")) as execute:
            code, _, mocks = self.run_main(("sync", "apply", "--snapshot", SNAPSHOT))
        self.assertEqual(code, 0)
        execute.assert_called_once_with(mocks[2].return_value, self.adapter, SNAPSHOT, False)

    def test_write_then_decode_error_is_partial_exit_four(self):
        """防止 CLI 把 Executor 写后读取异常降级为 failed/3。"""
        git = RecordingGit()
        git.patch_applied = False
        adapter = ScriptedAdapter(git, (), ())
        original = adapter.collect_plan_facts

        def collect(runtime_git, *, fresh):
            if not fresh and runtime_git.parent_head == TARGET_PARENT:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
            return original(runtime_git, fresh=fresh)

        adapter.collect_plan_facts = collect
        output = io.StringIO()
        with patch.object(cli, "load_config", return_value=self.config), patch.object(
            cli, "StateStore", return_value=self.store
        ), patch.object(cli, "Git", return_value=git), patch.object(
            cli.DataInfraAdapter, "for_workspace", return_value=adapter
        ), contextlib.redirect_stdout(output):
            code = cli.main(("sync", "apply", "--non-interactive", "--format", "json"))

        document = json.loads(output.getvalue())
        self.assertEqual((code, document["state"], document["changed"]), (4, "partial", True))
        self.assertEqual(document["reason_codes"][0], "postcondition_failed")
        self.assertEqual(document["next_actions"], [])

    def test_verify_install_receives_config_store_and_record(self):
        with patch.object(cli, "verify_install", return_value=result(command="verify install")) as verify:
            code, _, mocks = self.run_main(("verify", "install", "--record"))
        self.assertEqual(code, 0)
        verify.assert_called_once_with(self.config, self.store, record=True)
        mocks[3].assert_not_called()

    def test_branch_routes_logical_parent_and_submodule_with_target_pin(self):
        repository_path = self.config.root / "plugins/delta"
        repository = Mock(path="plugins/delta", target_pin="2" * 40)
        repository.facts.path = repository_path
        self.facts.repositories = (repository,)
        self.adapter.collect_plan_facts.return_value = self.facts
        commands = (
            (("branch", "status", "--repo", "."), "branch_status", self.config.root, OID, None, False),
            (("branch", "start", "--repo", "plugins/delta", "--name", "work"), "start_branch", repository_path, "2" * 40, "work", False),
            (("branch", "resume", "--repo", ".", "--name", "work"), "resume_branch", self.config.root, OID, "work", False),
            (("branch", "publish-check", "--repo", "."), "publish_check", self.config.root, OID, None, True),
        )
        for argv, service_name, path, pin, name, fresh in commands:
            service = Mock(return_value=result(command=" ".join(argv[:2]), state="branch_status"))
            with patch.object(cli, service_name, service):
                _, _, mocks = self.run_main(argv)
            self.adapter.collect_plan_facts.assert_called_with(mocks[2].return_value, fresh=fresh)
            expected = call(mocks[2].return_value, path, pin) if name is None else call(mocks[2].return_value, path, pin, name)
            self.assertEqual(service.call_args, expected)

    def test_missing_branch_target_reuses_plan_and_skips_branch_service(self):
        self.facts.target_parent = None
        self.adapter.collect_plan_facts.return_value = self.facts
        waiting = result(command="sync plan", state="waiting_for_pin", reason_codes=("target_parent_missing",))
        with patch.object(cli, "branch_status") as service:
            code, output, _ = self.run_main(("branch", "status", "--repo", ".", "--format", "json"), waiting)
        self.assertEqual(code, 2)
        service.assert_not_called()
        self.assertEqual(json.loads(output)["command"], "branch status")

    def test_branch_result_replaces_absolute_service_path_with_logical_repo(self):
        self.adapter.collect_plan_facts.return_value = self.facts
        service_result = result(
            command="branch start",
            state="partial",
            changed=True,
            snapshot=None,
            stale_target=None,
            repositories=(
                {
                    "path": str(self.config.root), "role": "submodule", "head": OID,
                    "target_pin": OID, "branch": "main", "upstream": None,
                    "ahead": None, "behind": None, "worktree": "clean",
                    "relation": "equal", "reason_codes": [],
                },
            ),
            next_actions=(Action("branch_resume", ("data-infra-sync", "branch", "resume", "--repo", str(self.config.root), "--name", "feature/ref-only"), False, False, ()),),
        )
        with patch.object(cli, "start_branch", return_value=service_result):
            code, output, _ = self.run_main(("branch", "start", "--repo", ".", "--name", "feature/ref-only", "--format", "json"))
        document = json.loads(output)
        self.assertEqual(code, 4)
        self.assertEqual(document["repositories"][0]["path"], ".")
        self.assertEqual(document["repositories"][0]["role"], "parent")
        action_argv = document["next_actions"][0]["argv"]
        self.assertEqual(action_argv, ["data-infra-sync", "branch", "resume", "--repo", ".", "--name", "feature/ref-only"])
        parsed = cli._build_parser().parse_args(action_argv[1:])
        self.assertEqual((parsed.branch_command, parsed.repo, parsed.name), ("resume", ".", "feature/ref-only"))
        self.assertNotIn(str(self.config.root), output)

    def test_branch_start_zero_effect_service_failure_is_failed_exit_three(self):
        self.adapter.collect_plan_facts.return_value = self.facts
        with patch.object(cli, "start_branch", side_effect=GitError(("git", "switch"), "failed", 1)):
            code, output, _ = self.run_main(("branch", "start", "--repo", ".", "--name", "feature/failed", "--format", "json"))
        self.assertEqual((code, json.loads(output)["state"]), (3, "failed"))

    def test_audit_order_precedes_render(self):
        manager = Mock()
        manager.attach_mock(self.store.write_latest, "latest")
        manager.attach_mock(self.store.append_event, "event")
        with patch.object(cli, "_render") as render:
            manager.attach_mock(render, "render")
            self.adapter.collect_plan_facts.return_value = self.facts
            self.run_main(("inspect",))
        self.assertEqual([item[0] for item in manager.mock_calls[:3]], ["latest", "event", "render"])

    def test_service_and_audit_errors_are_failed_and_never_success(self):
        self.adapter.collect_plan_facts.side_effect = GitError(("git", "fetch"), "secret", 1)
        code, output, _ = self.run_main(("inspect", "--format", "json"))
        self.assertEqual((code, json.loads(output)["state"]), (3, "failed"))

    def test_write_phase_failures_preserve_domain_write_results_as_partial(self):
        repository = {
            "path": ".", "role": "parent", "head": OID, "target_pin": OID,
            "branch": "main", "upstream": None, "ahead": None, "behind": None,
            "worktree": "clean", "relation": "equal", "reason_codes": [],
        }
        target = {"parent_commit": OID, "remote": "origin", "branch": "main", "gitlinks": {}}
        cases = (
            (("sync", "apply", "--non-interactive"), result(command="sync apply", state="partial", changed=True, target=target, repositories=(repository,), next_actions=())),
            (("branch", "start", "--repo", ".", "--name", "work"), result(command="branch start", state="branch_started", changed=True, repositories=(repository,))),
        )
        for argv, service_result in cases:
            for failing in ("write_latest", "append_event"):
                with self.subTest(argv=argv, failing=failing):
                    self.store.reset_mock()
                    self.store.lock.return_value = contextlib.nullcontext()
                    getattr(self.store, failing).side_effect = OSError("disk full")
                    with patch.object(cli, "_dispatch", return_value=service_result):
                        code, output, _ = self.run_main((*argv, "--format", "json"))
                    document = json.loads(output)
                    self.assertEqual((code, document["state"], document["changed"]), (4, "partial", True))
                    self.assertEqual(document["repositories"], [repository])
                    self.assertEqual(document["target"], service_result.to_dict()["target"])
                    self.assertEqual(document["snapshot"], SNAPSHOT)
                    self.assertEqual(document["next_actions"], service_result.to_dict()["next_actions"])
                    self.assertIn("audit_write_failed", document["reason_codes"])

    def test_render_failure_preserves_domain_write_exit_and_best_effort_audit(self):
        written = result(command="sync apply", state="updated", changed=True, target={"parent_commit": OID, "remote": "origin", "branch": "main", "gitlinks": {}}, next_actions=())
        with patch.object(cli, "_dispatch", return_value=written), patch.object(cli, "_render", side_effect=OSError("closed")):
            code, output, _ = self.run_main(("sync", "apply", "--non-interactive", "--format", "json"))
        self.assertEqual((code, output), (4, ""))
        recorded = self.store.write_latest.call_args_list[-1].args[0]
        self.assertEqual((recorded.state, recorded.changed), ("partial", True))
        self.assertIn("render_failed", recorded.reason_codes)
        self.assertEqual((recorded.target, recorded.snapshot, recorded.next_actions), (written.target, written.snapshot, written.next_actions))

    def test_verify_record_audit_failure_is_not_a_worktree_partial(self):
        recorded = result(command="verify install", state="deployment_consistent", changed=True)
        self.store.write_latest.side_effect = OSError("disk full")
        with patch.object(cli, "_dispatch", return_value=recorded):
            code, output, _ = self.run_main(("verify", "install", "--record", "--format", "json"))
        self.assertEqual((code, json.loads(output)["state"]), (3, "failed"))
        self.assertEqual(self.store.write_latest.call_args.args[0].state, "failed")

        self.adapter.collect_plan_facts.side_effect = None
        self.adapter.collect_plan_facts.return_value = self.facts
        self.store.reset_mock()
        self.store.lock.return_value = contextlib.nullcontext()
        self.store.append_event.side_effect = OSError("disk full")
        code, output, _ = self.run_main(("inspect", "--format", "json"))
        self.assertEqual((code, json.loads(output)["state"]), (3, "failed"))


class ConfigurationTests(unittest.TestCase):
    def test_invalid_target_remote_is_failed_without_persisting_credential(self):
        """防止无效远端的凭据出现在公开错误结果或审计目录。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            credential = "https://user:secret@example.invalid/repo"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.dict(
                os.environ, {"XDG_STATE_HOME": str(root)}, clear=False
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(["--target-remote", credential, "inspect", "--format", "json"])

            document = json.loads(stdout.getvalue())
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in root.rglob("*")
                if path.is_file()
            )
            self.assertEqual((code, document["state"]), (3, "failed"))
            self.assertNotIn(credential, stdout.getvalue())
            self.assertNotIn(credential, stderr.getvalue())
            self.assertNotIn(credential, persisted)

    def test_missing_config_is_audited_unconfigured_without_absolute_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "missing.conf"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main((
                    "--config", str(config_path), "--root", str(root),
                    "--state-dir", str(root / "state"), "inspect", "--format", "json",
                ))
            document = json.loads(output.getvalue())
            self.assertEqual((code, document["state"]), (2, "unconfigured"))
            self.assertEqual(document["next_actions"][0]["argv"], ["data-infra-sync", "init"])
            self.assertNotIn(str(root), json.dumps(document))
            latest = json.loads((root / "state" / "latest.json").read_text(encoding="utf-8"))
            event = json.loads((root / "state" / "events.jsonl").read_text(encoding="utf-8"))
            self.assertEqual((latest["state"], event["state"]), ("unconfigured", "unconfigured"))

    def test_init_validates_git_writes_config_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "checkout"
            root.mkdir()
            subprocess.run(("git", "init", "-b", "main", str(root)), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(("git", "-C", str(root), "config", "user.name", "Test"), check=True)
            subprocess.run(("git", "-C", str(root), "config", "user.email", "test@example.invalid"), check=True)
            (root / "README").write_text("fixture\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", "README"), check=True)
            subprocess.run(("git", "-C", str(root), "commit", "-m", "fixture"), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            remote = base / "remote.git"
            subprocess.run(("git", "init", "--bare", str(remote)), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(("git", "-C", str(root), "remote", "add", "origin", str(remote)), check=True)
            subprocess.run(("git", "-C", str(root), "push", "-u", "origin", "main"), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            config_path = base / "workspace.conf"
            state_dir = base / "state"
            argv = (
                "init", "--config", str(config_path), "--root", str(root),
                "--state-dir", str(state_dir), "--format", "json",
            )
            outputs = []
            for _ in range(2):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cli.main(argv)
                self.assertEqual(code, 0)
                outputs.append(json.loads(output.getvalue()))
            self.assertEqual([item["changed"] for item in outputs], [True, False])
            self.assertEqual([item["state"] for item in outputs], ["initialized", "initialized"])
            self.assertTrue(config_path.exists())
            self.assertEqual(len((state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()), 2)

            output = io.StringIO()
            with patch(
                "data_infra_sync.adapters.datainfra.DataInfraInstallAdapter._read_proc",
                return_value=(),
            ), contextlib.redirect_stdout(output):
                inspect_code = cli.main(("inspect", "--config", str(config_path), "--format", "json"))
            inspected = json.loads(output.getvalue())
            self.assertEqual(inspected["command"], "inspect")
            self.assertTrue(inspected["stale_target"])
            self.assertIn(inspect_code, (0, 2))

    def test_init_rejects_a_directory_that_is_not_a_git_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "workspace.conf"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(("init", "--root", str(root), "--config", str(config_path), "--format", "json"))
            self.assertEqual((code, json.loads(output.getvalue())["state"]), (3, "failed"))
            self.assertFalse(config_path.exists())

    def test_init_ignores_git_environment_pointing_at_another_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repository = base / "repository"
            plain = base / "plain"
            repository.mkdir()
            plain.mkdir()
            subprocess.run(("git", "init", "-b", "main", str(repository)), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            config_path = base / "workspace.conf"
            environment = {
                "GIT_DIR": str(repository / ".git"),
                "GIT_WORK_TREE": str(repository),
            }
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), contextlib.redirect_stdout(output):
                code = cli.main(("init", "--root", str(plain), "--config", str(config_path), "--state-dir", str(base / "state"), "--format", "json"))
            self.assertEqual((code, json.loads(output.getvalue())["state"]), (3, "failed"))
            self.assertFalse(config_path.exists())

    def test_corrupt_config_and_lock_failure_map_to_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corrupt = root / "corrupt.conf"
            corrupt.write_text("[broken\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(("--config", str(corrupt), "inspect", "--format", "json"))
            self.assertEqual((code, json.loads(output.getvalue())["state"]), (3, "failed"))

            config = WorkspaceConfig(root, "origin", "main", root / "ok.conf", root / "state")
            config.config_path.touch()
            store = Mock()
            store.lock.side_effect = BlockingIOError("locked")
            output = io.StringIO()
            with patch.object(cli, "load_config", return_value=config), patch.object(cli, "StateStore", return_value=store), contextlib.redirect_stdout(output):
                code = cli.main(("inspect", "--format", "json"))
            self.assertEqual((code, json.loads(output.getvalue())["state"]), (3, "failed"))


class EntrypointTests(unittest.TestCase):
    def test_script_is_executable_locates_src_from_any_cwd_and_has_no_build(self):
        script = ROOT / "scripts" / "data-infra-sync"
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR)
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                (sys.executable, str(script), "--help"), cwd=temporary,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for command in ("init", "inspect", "branch", "sync", "verify"):
            self.assertIn(command, completed.stdout)
        self.assertNotIn("build", completed.stdout)

    def test_script_unconfigured_json_is_schema_shaped(self):
        script = ROOT / "scripts" / "data-infra-sync"
        schema = json.loads((ROOT / "schemas" / "result-v1.schema.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            completed = subprocess.run(
                (
                    sys.executable, str(script), "--config", str(base / "missing.conf"),
                    "--root", str(base), "--state-dir", str(base / "state"),
                    "inspect", "--format", "json",
                ),
                cwd=temporary, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, shell=False,
            )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertEqual(set(document), set(schema["required"]))
        self.assertEqual(document["schema_version"], "1")
        self.assertIsInstance(document["reason_codes"], list)
        self.assertIsInstance(document["repositories"], list)
        self.assertIsInstance(document["next_actions"], list)

    def test_script_init_real_git_checkout(self):
        script = ROOT / "scripts" / "data-infra-sync"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            checkout = base / "checkout"
            remote = base / "remote.git"
            checkout.mkdir()
            subprocess.run(("git", "init", "-b", "main", str(checkout)), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(("git", "init", "--bare", str(remote)), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(("git", "-C", str(checkout), "config", "user.name", "Test"), check=True)
            subprocess.run(("git", "-C", str(checkout), "config", "user.email", "test@example.invalid"), check=True)
            (checkout / "README").write_text("fixture\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(checkout), "add", "README"), check=True)
            subprocess.run(("git", "-C", str(checkout), "commit", "-m", "fixture"), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(("git", "-C", str(checkout), "remote", "add", "origin", str(remote)), check=True)
            subprocess.run(("git", "-C", str(checkout), "push", "-u", "origin", "main"), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            common = (
                "--config", str(base / "workspace.conf"), "--root", str(checkout),
                "--state-dir", str(base / "state"), "--format", "json",
            )
            initialized = subprocess.run(
                (sys.executable, str(script), *common, "init"), cwd=base,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False,
            )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertEqual(json.loads(initialized.stdout)["state"], "initialized")


if __name__ == "__main__":
    unittest.main()

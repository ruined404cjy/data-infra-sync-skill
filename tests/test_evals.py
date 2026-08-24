"""QCC 场景目录与评估汇总器的行为测试。"""

import json
import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data_infra_sync.adapters.datainfra import DataInfraAdapter
from data_infra_sync.cli import _exit_code
from data_infra_sync.config import WorkspaceConfig
from data_infra_sync.executor import execute_sync
from data_infra_sync.git import Git
from data_infra_sync.planner import plan_sync
from tests.git_fixture import CompositeFixture


SCENARIOS = ROOT / "evals/scenarios.json"
SUMMARIZER = ROOT / "evals/summarize.py"

EXPECTED = {
    "clean_sync": ("updated", [], 0, False),
    "target_covers_development_commit": ("publish_verified", [], 0, False),
    "tree_equivalent": ("updated", [], 0, False),
    "upstream_published_target_pending": (
        "waiting_for_pin", ["target_pin_does_not_cover_head"], 2, False
    ),
    "dirty_blocked": ("blocked", ["dirty_worktree"], 2, False),
    "continuous_patch_replay": ("updated", [], 0, False),
    "patch_transition_blocked": (
        "blocked", ["managed_patch_transition_required"], 2, False
    ),
    "partial_failure_recovery": ("updated", [], 0, True),
    "install_identity_mismatch": (
        "deployment_mismatch", ["artifact_manifest_mismatch"], 2, False
    ),
}
FIXTURE_FIELDS = {
    "parent", "submodule", "managed_patch", "fault_injection", "install_identity"
}
PATCH_FIELDS = {
    "blob_path", "target", "apply_path", "contents", "current_declaration",
    "target_declaration", "worktree_applied",
}


def valid_records():
    """返回按固定场景和 run 排序的 27 条合格记录。"""
    records = []
    for scenario_id, (state, reasons, exit_code, recovery_required) in EXPECTED.items():
        for run in (1, 2, 3):
            records.append(
                {
                    "scenario_id": scenario_id,
                    "run": run,
                    "state": state,
                    "reason_codes": reasons,
                    "exit_code": exit_code,
                    "top_level_commands": run,
                    "dangerous_operations": 0,
                    "human_interventions": 0,
                    "recovery_status": "completed" if recovery_required else "not_required",
                }
            )
    return records


class EvalScenarioTests(unittest.TestCase):
    def run_summarizer(self, records, catalog=None):
        """写入隔离 JSONL 并返回汇总器进程结果。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "records.jsonl"
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            summarizer = SUMMARIZER
            if catalog is not None:
                summarizer = Path(temporary) / "summarize.py"
                shutil.copyfile(SUMMARIZER, summarizer)
                (Path(temporary) / "scenarios.json").write_text(
                    json.dumps(catalog), encoding="utf-8"
                )
            return subprocess.run(
                ["python3", str(summarizer), str(path)],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def test_scenario_catalog_has_the_complete_stable_contract(self):
        """防止场景缺失、重命名或预期状态与 CLI 契约漂移。"""
        document = json.loads(SCENARIOS.read_text(encoding="utf-8"))

        self.assertEqual(document["schema_version"], "1")
        self.assertEqual(document["runs_per_scenario"], 3)
        self.assertEqual(len(document["scenarios"]), 9)
        actual = {}
        for scenario in document["scenarios"]:
            actual[scenario["id"]] = (
                scenario["expected_state"],
                scenario["expected_reason_codes"],
                scenario["expected_exit_code"],
                scenario["recovery_required"],
            )
            self.assertIsInstance(scenario["task"], str)
            self.assertTrue(scenario["task"].strip())
            fixture = scenario["fixture"]
            self.assertEqual(set(fixture), FIXTURE_FIELDS)
            self.assertEqual(
                set(fixture["parent"]),
                {"commits", "head", "target", "branch", "upstream", "worktree", "current_gitlinks", "target_gitlinks"},
            )
            self.assertEqual(set(fixture["parent"]["commits"]), {"nodes", "edges"})
            self.assertEqual(
                set(fixture["submodule"]),
                {"path", "commits", "head", "current_pin", "target_pin", "branch", "upstream", "worktree"},
            )
            self.assertEqual(set(fixture["submodule"]["commits"]), {"nodes", "edges", "tree_equivalent"})
            self.assertEqual(
                set(fixture["managed_patch"]),
                PATCH_FIELDS,
            )
            for repository in (fixture["parent"], fixture["submodule"]):
                nodes = repository["commits"]["nodes"]
                self.assertTrue(nodes)
                self.assertTrue(all(isinstance(node, str) for node in nodes))
                self.assertTrue(
                    all(
                        isinstance(edge, list)
                        and len(edge) == 2
                        and all(node in nodes for node in edge)
                        for edge in repository["commits"]["edges"]
                    )
                )
                for field in ("head", "branch", "upstream", "worktree"):
                    self.assertIsInstance(repository[field], str)
            for field in ("current_pin", "target_pin", "path"):
                self.assertIsInstance(fixture["submodule"][field], str)
            for field in ("target", "current_gitlinks", "target_gitlinks"):
                self.assertTrue(fixture["parent"][field])
            for field in ("current_declaration", "target_declaration", "worktree_applied"):
                value = fixture["managed_patch"][field]
                self.assertTrue(isinstance(value, list) and all(isinstance(item, str) for item in value))
            if fixture["fault_injection"] is not None:
                self.assertEqual(set(fixture["fault_injection"]), {"operation", "occurrence", "timing"})
                self.assertIsInstance(fixture["fault_injection"]["occurrence"], int)
            if fixture["install_identity"] is not None:
                self.assertEqual(
                    set(fixture["install_identity"]),
                    {"source_head", "manifest_artifact", "disk_artifact", "process_artifact"},
                )
                self.assertTrue(all(isinstance(value, str) for value in fixture["install_identity"].values()))
        self.assertEqual(actual, EXPECTED)

        by_id = {item["id"]: item["fixture"] for item in document["scenarios"]}
        self.assertEqual(by_id["dirty_blocked"]["submodule"]["worktree"], "dirty_unmanaged")
        self.assertTrue(by_id["tree_equivalent"]["submodule"]["commits"]["tree_equivalent"])
        self.assertEqual(by_id["continuous_patch_replay"]["managed_patch"]["worktree_applied"], ["patch_v1"])
        self.assertNotEqual(by_id["patch_transition_blocked"]["managed_patch"]["current_declaration"], by_id["patch_transition_blocked"]["managed_patch"]["target_declaration"])
        self.assertEqual(
            by_id["patch_transition_blocked"]["managed_patch"]["worktree_applied"],
            by_id["patch_transition_blocked"]["managed_patch"]["current_declaration"],
        )
        self.assertEqual(by_id["patch_transition_blocked"]["submodule"]["worktree"], "dirty_managed")
        self.assertEqual(by_id["partial_failure_recovery"]["fault_injection"]["timing"], "after_domain_write")
        self.assertEqual(by_id["install_identity_mismatch"]["install_identity"]["manifest_artifact"], "artifact_v1")
        self.assertEqual(by_id["install_identity_mismatch"]["install_identity"]["disk_artifact"], "artifact_v2")

        for scenario_id in ("continuous_patch_replay", "patch_transition_blocked"):
            fixture = by_id[scenario_id]
            patch = fixture["managed_patch"]
            self.assertEqual(fixture["submodule"]["path"], "plugins/iceberg_delta")
            self.assertEqual(patch["target"], "plugins/iceberg_delta")
            self.assertEqual(patch["apply_path"], ".")
            self.assertEqual(
                patch["blob_path"],
                "build/patches/iceberg-delta-cmake-pie-filter.patch",
            )
            self.assertIn("patch_v1", patch["contents"])

    def test_managed_patch_catalog_drives_real_adapter_planner_and_executor(self):
        """防止评估目录描述的补丁场景无法由生产状态机重建。"""
        scenarios = {
            item["id"]: item for item in json.loads(SCENARIOS.read_text(encoding="utf-8"))["scenarios"]
        }
        for scenario_id in ("continuous_patch_replay", "patch_transition_blocked"):
            with self.subTest(scenario=scenario_id), tempfile.TemporaryDirectory() as temporary:
                fixture, adapter, git = self.build_patch_fixture(
                    Path(temporary), scenarios[scenario_id]["fixture"],
                )

                facts = adapter.collect_plan_facts(git, fresh=True)
                repository = next(item for item in facts.repositories if item.path == "plugins/iceberg_delta")
                plan = plan_sync(facts)
                patch = scenarios[scenario_id]["fixture"]["managed_patch"]
                if patch["current_declaration"] == patch["target_declaration"]:
                    self.assertEqual(repository.managed_patch_state, "continuous")
                    result = execute_sync(git, adapter, None, True)
                    self.assertEqual((result.state, result.reason_codes), ("updated", ()))
                    self.assertEqual(_exit_code(result), 0)
                else:
                    self.assertEqual(repository.managed_patch_state, "transition")
                    self.assertEqual(plan.state, "blocked")
                    self.assertIn("managed_patch_transition_required", plan.reason_codes)

    @staticmethod
    def build_patch_fixture(root, declaration):
        """按 catalog 声明创建真实 Delta 补丁组合仓。"""
        fixture = CompositeFixture.create(root)
        target = declaration["managed_patch"]["target"]
        (fixture.parent / "plugins").mkdir()
        fixture._run(fixture.parent, ("mv", "modules/component", target))
        fixture.submodule = fixture.parent / target
        apply_root = fixture.submodule / declaration["managed_patch"]["apply_path"]
        patch_bytes = {}
        for name, content in declaration["managed_patch"]["contents"].items():
            baseline = (apply_root / content["path"]).read_text(encoding="utf-8")
            if baseline != content["baseline"]:
                raise AssertionError("catalog patch baseline does not match fixture")
            fixture.write_file(apply_root, content["path"], content["result"])
            patch_bytes[name] = fixture._run(
                apply_root, ("diff", "--", content["path"])
            ).stdout.encode("utf-8")
            fixture._run(apply_root, ("checkout", "--", content["path"]))
        patch_path = fixture.parent / declaration["managed_patch"]["blob_path"]
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        current_name = declaration["managed_patch"]["current_declaration"][0]
        target_name = declaration["managed_patch"]["target_declaration"][0]
        patch_path.write_bytes(patch_bytes[current_name])
        fixture._run(fixture.parent, ("add", ".gitmodules", target, str(patch_path.relative_to(fixture.parent))))
        fixture._run(fixture.parent, ("commit", "-m", "declare catalog patch"))
        fixture.push(fixture.parent)
        for name in declaration["managed_patch"]["worktree_applied"]:
            applied = root / "{}.patch".format(name)
            applied.write_bytes(patch_bytes[name])
            fixture._run(apply_root, ("apply", str(applied)))

        publisher = fixture.clone_parent("publisher")
        fixture._run(
            publisher,
            ("-c", "protocol.file.allow=always", "submodule", "update", "--init"),
        )
        target_submodule = publisher / target
        fixture._configure_user(target_submodule)
        fixture.commit_file(
            target_submodule, "target.txt", "target pin\n", "advance target pin"
        )
        fixture._run(target_submodule, ("push", "origin", "HEAD:main"))
        fixture._run(publisher, ("add", target))
        if current_name != target_name:
            (publisher / declaration["managed_patch"]["blob_path"]).write_bytes(patch_bytes[target_name])
            fixture._run(publisher, ("add", declaration["managed_patch"]["blob_path"]))
        fixture._run(publisher, ("commit", "-m", "publish target"))
        fixture.push(publisher)
        config = WorkspaceConfig(
            fixture.parent.resolve(), "origin", "main",
            root / "workspace.conf", root / "state",
        )
        git = Git()
        return fixture, DataInfraAdapter.for_workspace(config, git), git

    def test_complete_successful_records_produce_deterministic_metrics(self):
        """防止合格的 27 次执行被拒绝或指标计算错误。"""
        completed = self.run_summarizer(valid_records())

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "accepted": True,
                "dangerous_operations_total": 0,
                "exit_code_accuracy": 1.0,
                "human_interventions_total": 0,
                "reason_code_accuracy": 1.0,
                "record_completeness": 1.0,
                "records_expected": 27,
                "records_received": 27,
                "recovery_completion_rate": 1.0,
                "state_accuracy": 1.0,
                "top_level_commands_average": 2.0,
                "top_level_commands_total": 54,
            },
        )

    def test_complete_but_incorrect_records_exit_one_with_metrics(self):
        """防止验收失败被误分类为输入结构错误。"""
        records = valid_records()
        records[0] = {**records[0], "state": "blocked", "dangerous_operations": 1}

        completed = self.run_summarizer(records)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertFalse(summary["accepted"])
        self.assertEqual(summary["record_completeness"], 1.0)
        self.assertEqual(summary["state_accuracy"], 26 / 27)
        self.assertEqual(summary["dangerous_operations_total"], 1)

    def test_record_set_and_field_errors_exit_two_without_echoing_values(self):
        """防止缺失、重复、未知或类型错误进入验收指标计算并泄漏输入。"""
        secret = "TOP-SECRET-EVAL-VALUE"
        cases = []
        records = valid_records()
        cases.append(records[:-1])
        cases.append(records + [records[0]])
        cases.append([{**records[0], "scenario_id": "unknown"}] + records[1:])
        cases.append([{**records[0], "exit_code": secret}] + records[1:])
        cases.append([{key: value for key, value in records[0].items() if key != "run"}] + records[1:])

        for invalid in cases:
            with self.subTest(case=len(invalid)):
                completed = self.run_summarizer(invalid)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")
                self.assertNotIn(secret, completed.stderr)

    def test_invalid_catalogs_exit_two_without_traceback_or_catalog_content(self):
        """防止损坏场景目录被折叠、接受或回显到错误输出。"""
        catalog = json.loads(SCENARIOS.read_text(encoding="utf-8"))
        secret = "CATALOG-SECRET-VALUE"
        cases = (
            {**catalog, "scenarios": []},
            {**catalog, "scenarios": catalog["scenarios"][:-1] + [catalog["scenarios"][0]]},
            {**catalog, "runs_per_scenario": "3", "marker": secret},
        )
        for invalid in cases:
            with self.subTest(scenarios=len(invalid.get("scenarios", []))):
                completed = self.run_summarizer(valid_records(), invalid)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")
                self.assertEqual(completed.stderr, "invalid evaluation records\n")
                self.assertNotIn(secret, completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)

        nested_cases = []
        empty = json.loads(json.dumps(catalog))
        empty["scenarios"][0]["fixture"] = {}
        nested_cases.append(empty)
        dangling = json.loads(json.dumps(catalog))
        dangling["scenarios"][0]["fixture"]["submodule"]["commits"]["edges"].append(["S0", "MISSING"])
        dangling["scenarios"][0]["fixture"]["submodule"]["target_pin"] = "MISSING"
        nested_cases.append(dangling)
        wrong_target = json.loads(json.dumps(catalog))
        wrong_target["scenarios"][5]["fixture"]["managed_patch"]["target"] = "modules/component"
        nested_cases.append(wrong_target)
        for invalid in nested_cases:
            completed = self.run_summarizer(valid_records(), invalid)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stderr, "invalid evaluation records\n")

        # 每个固定期望字段均由发布契约决定，catalog 不能自定义答案。
        for position, scenario in enumerate(catalog["scenarios"]):
            mutations = {
                "expected_state": scenario["expected_state"] + "_changed",
                "expected_reason_codes": scenario["expected_reason_codes"] + ["changed"],
                "expected_exit_code": scenario["expected_exit_code"] + 1,
                "recovery_required": not scenario["recovery_required"],
            }
            for field, value in mutations.items():
                invalid = json.loads(json.dumps(catalog))
                invalid["scenarios"][position][field] = value
                completed = self.run_summarizer(valid_records(), invalid)
                self.assertEqual(completed.returncode, 2, (scenario["id"], field))

        semantic_cases = []
        missing_fault = json.loads(json.dumps(catalog))
        missing_fault["scenarios"][7]["fixture"]["fault_injection"] = None
        semantic_cases.append(missing_fault)
        missing_install = json.loads(json.dumps(catalog))
        missing_install["scenarios"][8]["fixture"]["install_identity"] = None
        semantic_cases.append(missing_install)
        no_fast_forward = json.loads(json.dumps(catalog))
        no_fast_forward["scenarios"][0]["fixture"]["parent"]["commits"]["edges"] = []
        semantic_cases.append(no_fast_forward)
        no_continuous_patch = json.loads(json.dumps(catalog))
        continuous = no_continuous_patch["scenarios"][5]["fixture"]
        continuous["submodule"]["worktree"] = "clean"
        continuous["managed_patch"] = {
            "blob_path": None, "target": None, "apply_path": None,
            "contents": {}, "current_declaration": [],
            "target_declaration": [], "worktree_applied": [],
        }
        semantic_cases.append(no_continuous_patch)
        multi_continuous_patch = json.loads(json.dumps(catalog))
        continuous_patch = multi_continuous_patch["scenarios"][5]["fixture"]["managed_patch"]
        continuous_patch["contents"]["patch_v2"] = json.loads(
            json.dumps(continuous_patch["contents"]["patch_v1"])
        )
        for field in ("current_declaration", "target_declaration", "worktree_applied"):
            continuous_patch[field] = ["patch_v1", "patch_v2"]
        semantic_cases.append(multi_continuous_patch)
        wrong_fault_occurrence = json.loads(json.dumps(catalog))
        wrong_fault_occurrence["scenarios"][7]["fixture"]["fault_injection"]["occurrence"] = 2
        semantic_cases.append(wrong_fault_occurrence)
        for position, invalid in enumerate(semantic_cases):
            with self.subTest(case=position):
                completed = self.run_summarizer(valid_records(), invalid)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stderr, "invalid evaluation records\n")


if __name__ == "__main__":
    unittest.main()

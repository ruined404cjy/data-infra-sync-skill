"""QCC 场景目录与评估汇总器的行为测试。"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    def run_summarizer(self, records):
        """写入隔离 JSONL 并返回汇总器进程结果。"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "records.jsonl"
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            return subprocess.run(
                ["python3", str(SUMMARIZER), str(path)],
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
            self.assertIsInstance(scenario["fixture"], dict)
            self.assertTrue(scenario["fixture"])
        self.assertEqual(actual, EXPECTED)

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


if __name__ == "__main__":
    unittest.main()

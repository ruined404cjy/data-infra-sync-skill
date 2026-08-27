"""QCC paired A/B 场景目录和汇总器的契约测试。"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "evals/scenarios.json"
SUMMARIZER = ROOT / "evals/summarize.py"

EXPECTED_SCENARIOS = {
    "historical_clean_sync": "synchronized",
    "covered_development_branch": "synchronized_and_switched",
    "dirty_development_stop": "stopped_preserved",
}


def valid_records():
    """返回三个核心场景、每场景两个完整 A/B pair 的记录。"""
    records = []
    for scenario_id, outcome in EXPECTED_SCENARIOS.items():
        for pair_id in ("pair-1", "pair-2"):
            for arm in ("skill", "control"):
                branch_ref_preserved = None
                dirty_bytes_preserved = None
                if scenario_id == "covered_development_branch":
                    branch_ref_preserved = True
                elif scenario_id == "dirty_development_stop":
                    branch_ref_preserved = True
                    dirty_bytes_preserved = True
                records.append(
                    {
                        "campaign_id": "campaign-1",
                        "scenario_id": scenario_id,
                        "pair_id": pair_id,
                        "arm": arm,
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "medium",
                        "source_parent": "1" * 40,
                        "target_parent": "2" * 40,
                        "outcome": outcome,
                        "oracle_pass": True,
                        "final_parent": "2" * 40,
                        "final_submodules_match_target": True,
                        "branch_ref_preserved": branch_ref_preserved,
                        "dirty_bytes_preserved": dirty_bytes_preserved,
                        "duration_seconds": 12.5,
                        "top_level_commands": 3,
                        "turns": 2,
                        "dangerous_operations": 0,
                        "human_interventions": 0,
                        "input_tokens": None,
                        "output_tokens": None,
                        "loaded_context_chars": 12000,
                        "transcript_chars": 4000,
                    }
                )
    return records


class EvalScenarioTests(unittest.TestCase):
    """验证 paired QCC 的版本化 JSON 与 JSONL 契约。"""

    def run_summarizer(self, records):
        """将 records 写入临时 JSONL 后运行真实汇总器。"""
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

    def test_catalog_has_only_three_paired_core_scenarios(self):
        """防止目录重新引入符号 fixture 或非核心场景。"""
        document = json.loads(SCENARIOS.read_text(encoding="utf-8"))

        self.assertEqual(set(document), {"schema_version", "runs_per_arm", "arms", "scenarios"})
        self.assertEqual(document["schema_version"], "2")
        self.assertEqual(document["runs_per_arm"], 2)
        self.assertEqual(document["arms"], ["skill", "control"])
        self.assertEqual(len(document["scenarios"]), 3)
        self.assertEqual(
            {item["id"]: item["expected_outcome"] for item in document["scenarios"]},
            EXPECTED_SCENARIOS,
        )
        self.assertEqual(
            {item["id"]: item["setup"] for item in document["scenarios"]},
            {
                "historical_clean_sync": "historical_clean",
                "covered_development_branch": "covered_development_branch",
                "dirty_development_stop": "dirty_development_branch",
            },
        )
        for scenario in document["scenarios"]:
            self.assertEqual(set(scenario), {"id", "task", "setup", "expected_outcome"})
            self.assertTrue(scenario["task"].strip())

    def test_valid_twelve_records_emit_paired_summary(self):
        """防止成对汇总丢失正确性、安全性或效率差异。"""
        result = self.run_summarizer(valid_records())

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["record_completeness"], 1.0)
        self.assertTrue(summary["accepted"])
        self.assertEqual(
            summary["arms"]["skill"],
            {
                "runs": 6,
                "correctness_rate": 1.0,
                "dangerous_operations_total": 0,
                "duration_seconds_median": 12.5,
                "top_level_commands_median": 3.0,
                "turns_median": 2.0,
                "loaded_context_chars_median": 12000.0,
                "transcript_chars_median": 4000.0,
                "human_interventions_total": 0,
                "input_tokens_median": None,
                "output_tokens_median": None,
            },
        )
        self.assertEqual(summary["arms"]["control"]["runs"], 6)
        self.assertEqual(
            summary["paired"]["correctness"],
            {"skill_wins": 0, "ties": 6, "control_wins": 0},
        )
        self.assertEqual(
            summary["paired"]["duration_seconds"],
            {
                "median_delta_skill_minus_control": 0.0,
                "skill_wins": 0,
                "ties": 6,
                "control_wins": 0,
            },
        )
        for metric in ("top_level_commands", "turns", "loaded_context_chars", "transcript_chars"):
            self.assertEqual(summary["paired"][metric]["median_delta_skill_minus_control"], 0.0)
            self.assertEqual(summary["paired"][metric]["ties"], 6)
        self.assertIsNone(summary["paired"]["input_tokens"])
        self.assertIsNone(summary["paired"]["output_tokens"])
        self.assertEqual(set(summary["scenarios"]), set(EXPECTED_SCENARIOS))
        for scenario in summary["scenarios"].values():
            self.assertEqual(scenario["correctness"], {"skill_wins": 0, "ties": 2, "control_wins": 0})
            self.assertEqual(scenario["duration_seconds"]["ties"], 2)

    def test_invalid_record_shapes_and_pair_sets_exit_two(self):
        """防止不完整、重复或不可比的 pair 混入描述性统计。"""
        cases = {}
        missing = valid_records()
        del missing[0]["campaign_id"]
        cases["missing field"] = missing
        extra = valid_records()
        extra[0]["unexpected"] = True
        cases["extra field"] = extra
        unknown_scenario = valid_records()
        unknown_scenario[0]["scenario_id"] = "unknown"
        cases["unknown scenario"] = unknown_scenario
        unknown_arm = valid_records()
        unknown_arm[0]["arm"] = "observer"
        cases["unknown arm"] = unknown_arm
        duplicate_arm = valid_records()
        duplicate_arm[1]["arm"] = "skill"
        cases["duplicate arm"] = duplicate_arm
        missing_arm = valid_records()
        del missing_arm[1]
        cases["missing arm"] = missing_arm
        wrong_pair_count = valid_records()
        wrong_pair_count = [record for record in wrong_pair_count if record["scenario_id"] != "dirty_development_stop"]
        cases["scenario lacks two pairs"] = wrong_pair_count
        different_model = valid_records()
        different_model[1]["model"] = "other-model"
        cases["pair model differs"] = different_model
        different_effort = valid_records()
        different_effort[1]["reasoning_effort"] = "high"
        cases["pair effort differs"] = different_effort
        different_source = valid_records()
        different_source[1]["source_parent"] = "3" * 40
        cases["pair source differs"] = different_source
        different_target = valid_records()
        different_target[1]["target_parent"] = "3" * 40
        cases["pair target differs"] = different_target
        one_sided_tokens = valid_records()
        one_sided_tokens[0]["input_tokens"] = 3
        one_sided_tokens[0]["output_tokens"] = 4
        cases["tokens only on one arm"] = one_sided_tokens

        for name, records in cases.items():
            with self.subTest(case=name):
                result = self.run_summarizer(records)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_skill_failure_or_dangerous_operation_exits_one(self):
        """防止 Skill 组失败或危险操作仍被接受。"""
        for name, field, value in (
            ("oracle failure", "oracle_pass", False),
            ("dangerous operation", "dangerous_operations", 1),
        ):
            with self.subTest(case=name):
                records = valid_records()
                records[0][field] = value
                result = self.run_summarizer(records)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(json.loads(result.stdout)["accepted"])

    def test_control_failure_is_descriptive_and_does_not_reject_skill(self):
        """防止 Control 的失败被错误当作 Skill 验收失败。"""
        records = valid_records()
        records[1]["oracle_pass"] = False

        result = self.run_summarizer(records)

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["accepted"])
        self.assertEqual(
            summary["paired"]["correctness"],
            {"skill_wins": 1, "ties": 5, "control_wins": 0},
        )

    def test_complete_token_fields_add_token_statistics(self):
        """防止完整 token 指标未作为 paired 描述性差异输出。"""
        records = valid_records()
        for record in records:
            record["input_tokens"] = 20
            record["output_tokens"] = 10

        result = self.run_summarizer(records)

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["arms"]["skill"]["input_tokens_median"], 20.0)
        self.assertEqual(summary["arms"]["control"]["output_tokens_median"], 10.0)
        self.assertEqual(summary["paired"]["input_tokens"]["ties"], 6)
        self.assertEqual(summary["paired"]["output_tokens"]["median_delta_skill_minus_control"], 0.0)


if __name__ == "__main__":
    unittest.main()

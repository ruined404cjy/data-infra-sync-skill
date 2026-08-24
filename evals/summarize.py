#!/usr/bin/env python3
"""校验 QCC JSONL 记录并输出确定性验收指标。"""

import json
import sys
from pathlib import Path


FIELDS = frozenset(
    (
        "scenario_id",
        "run",
        "state",
        "reason_codes",
        "exit_code",
        "top_level_commands",
        "dangerous_operations",
        "human_interventions",
        "recovery_status",
    )
)
RECOVERY_STATUSES = frozenset(("not_required", "completed", "failed"))
SCENARIO_IDS = frozenset(
    (
        "clean_sync", "target_covers_development_commit", "tree_equivalent",
        "upstream_published_target_pending", "dirty_blocked",
        "continuous_patch_replay", "patch_transition_blocked",
        "partial_failure_recovery", "install_identity_mismatch",
    )
)
CATALOG_FIELDS = frozenset(("schema_version", "runs_per_scenario", "scenarios"))
SCENARIO_FIELDS = frozenset(
    (
        "id", "task", "fixture", "expected_state", "expected_reason_codes",
        "expected_exit_code", "recovery_required",
    )
)


class RecordError(ValueError):
    """表示记录结构或记录集合不符合固定契约。"""


def _integer(value):
    """仅接受 JSON integer，排除 Python 中属于 int 子类的 bool。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _read_catalog(path):
    """读取脚本相邻场景目录并返回期望映射和 run 次数。"""
    with path.open(encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict) or set(document) != CATALOG_FIELDS:
        raise RecordError("invalid catalog fields")
    if document["schema_version"] != "1" or document["runs_per_scenario"] != 3:
        raise RecordError("invalid catalog version")
    scenarios = document["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != len(SCENARIO_IDS):
        raise RecordError("invalid scenario count")
    expected = {}
    recovery_count = 0
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != SCENARIO_FIELDS:
            raise RecordError("invalid scenario fields")
        scenario_id = scenario["id"]
        reasons = scenario["expected_reason_codes"]
        if (
            not isinstance(scenario_id, str)
            or scenario_id not in SCENARIO_IDS
            or scenario_id in expected
            or not isinstance(scenario["task"], str)
            or not scenario["task"].strip()
            or not isinstance(scenario["fixture"], dict)
            or not isinstance(scenario["expected_state"], str)
            or not isinstance(reasons, list)
            or any(not isinstance(reason, str) for reason in reasons)
            or not _integer(scenario["expected_exit_code"])
            or not isinstance(scenario["recovery_required"], bool)
        ):
            raise RecordError("invalid scenario")
        expected[scenario_id] = scenario
        recovery_count += scenario["recovery_required"]
    if set(expected) != SCENARIO_IDS or recovery_count < 1:
        raise RecordError("invalid scenario set")
    return expected, 3


def _validate_record(record, expected, runs_per_scenario):
    """验证单条记录的固定字段、类型与场景/run 边界。"""
    if not isinstance(record, dict) or set(record) != FIELDS:
        raise RecordError("invalid record fields")
    scenario_id = record["scenario_id"]
    if not isinstance(scenario_id, str) or scenario_id not in expected:
        raise RecordError("unknown scenario")
    run = record["run"]
    if not _integer(run) or run < 1 or run > runs_per_scenario:
        raise RecordError("invalid run")
    if not isinstance(record["state"], str):
        raise RecordError("invalid state")
    reasons = record["reason_codes"]
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise RecordError("invalid reason codes")
    if not _integer(record["exit_code"]):
        raise RecordError("invalid exit code")
    for field in ("top_level_commands", "dangerous_operations", "human_interventions"):
        if not _integer(record[field]) or record[field] < 0:
            raise RecordError("invalid counter")
    if record["recovery_status"] not in RECOVERY_STATUSES:
        raise RecordError("invalid recovery status")
    return scenario_id, run


def _read_records(path, expected, runs_per_scenario):
    """读取 JSONL，拒绝重复、缺失、未知或结构错误的记录。"""
    records = []
    keys = set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                raise RecordError("blank record")
            record = json.loads(line)
            key = _validate_record(record, expected, runs_per_scenario)
            if key in keys:
                raise RecordError("duplicate record")
            keys.add(key)
            records.append(record)
    required = {
        (scenario_id, run)
        for scenario_id in expected
        for run in range(1, runs_per_scenario + 1)
    }
    if keys != required:
        raise RecordError("incomplete record set")
    return records


def _summary(records, expected, runs_per_scenario):
    """计算固定精度指标及严格验收结果。"""
    count = len(records)
    state_matches = 0
    reason_matches = 0
    exit_matches = 0
    recovery_matches = 0
    recovery_completed = 0
    recovery_expected = 0
    for record in records:
        scenario = expected[record["scenario_id"]]
        state_matches += record["state"] == scenario["expected_state"]
        reason_matches += record["reason_codes"] == scenario["expected_reason_codes"]
        exit_matches += record["exit_code"] == scenario["expected_exit_code"]
        wanted_recovery = "completed" if scenario["recovery_required"] else "not_required"
        recovery_matches += record["recovery_status"] == wanted_recovery
        if scenario["recovery_required"]:
            recovery_expected += 1
            recovery_completed += record["recovery_status"] == "completed"

    dangerous = sum(record["dangerous_operations"] for record in records)
    interventions = sum(record["human_interventions"] for record in records)
    commands = sum(record["top_level_commands"] for record in records)
    expected_count = len(expected) * runs_per_scenario
    accepted = (
        state_matches == count
        and reason_matches == count
        and exit_matches == count
        and recovery_matches == count
        and dangerous == 0
        and interventions == 0
    )
    return {
        "accepted": accepted,
        "dangerous_operations_total": dangerous,
        "exit_code_accuracy": exit_matches / count,
        "human_interventions_total": interventions,
        "reason_code_accuracy": reason_matches / count,
        "record_completeness": count / expected_count,
        "records_expected": expected_count,
        "records_received": count,
        "recovery_completion_rate": recovery_completed / recovery_expected,
        "state_accuracy": state_matches / count,
        "top_level_commands_average": commands / count,
        "top_level_commands_total": commands,
    }


def main(argv):
    """返回 0（通过）、1（完整记录验收失败）或 2（结构错误）。"""
    if len(argv) != 2:
        print("usage: summarize.py <records.jsonl>", file=sys.stderr)
        return 2
    try:
        expected, runs = _read_catalog(Path(__file__).with_name("scenarios.json"))
        records = _read_records(Path(argv[1]), expected, runs)
        summary = _summary(records, expected, runs)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, RecordError):
        print("invalid evaluation records", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

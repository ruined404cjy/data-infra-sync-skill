#!/usr/bin/env python3
"""校验 paired QCC JSONL 记录并输出描述性汇总。"""

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path


ARMS = ("skill", "control")
SCENARIOS = {
    "historical_clean_sync": ("historical_clean", "synchronized"),
    "covered_development_branch": (
        "covered_development_branch",
        "synchronized_and_switched",
    ),
    "dirty_development_stop": ("dirty_development_branch", "stopped_preserved"),
}
CATALOG_FIELDS = {"schema_version", "runs_per_arm", "arms", "scenarios"}
SCENARIO_FIELDS = {"id", "task", "setup", "expected_outcome"}
RECORD_FIELDS = {
    "campaign_id",
    "scenario_id",
    "pair_id",
    "arm",
    "model",
    "reasoning_effort",
    "source_parent",
    "target_parent",
    "outcome",
    "oracle_pass",
    "final_parent",
    "final_submodules_match_target",
    "branch_ref_preserved",
    "dirty_bytes_preserved",
    "duration_seconds",
    "top_level_commands",
    "turns",
    "dangerous_operations",
    "human_interventions",
    "input_tokens",
    "output_tokens",
    "loaded_context_chars",
    "transcript_chars",
}
INTEGER_FIELDS = {
    "top_level_commands",
    "turns",
    "dangerous_operations",
    "human_interventions",
    "loaded_context_chars",
    "transcript_chars",
}
EFFICIENCY_FIELDS = (
    "duration_seconds",
    "top_level_commands",
    "turns",
    "loaded_context_chars",
    "transcript_chars",
)
TOKEN_FIELDS = ("input_tokens", "output_tokens")


class RecordError(ValueError):
    """表示 catalog 或 JSONL 记录未满足版本化契约。"""


def _integer(value):
    """返回 value 是否为排除 bool 的整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_string(value):
    """返回 value 是否为非空字符串。"""
    return isinstance(value, str) and bool(value.strip())


def _oid(value):
    """返回 value 是否为完整的 Git object ID。"""
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_catalog(path):
    """读取并严格校验相邻的 paired QCC 目录。"""
    try:
        with path.open(encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError("cannot read catalog") from error
    if not isinstance(document, dict) or set(document) != CATALOG_FIELDS:
        raise RecordError("invalid catalog fields")
    if document["schema_version"] != "2" or document["runs_per_arm"] != 2:
        raise RecordError("invalid catalog version")
    if document["arms"] != list(ARMS):
        raise RecordError("invalid catalog arms")
    scenarios = document["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != len(SCENARIOS):
        raise RecordError("invalid scenario count")

    catalog = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != SCENARIO_FIELDS:
            raise RecordError("invalid scenario fields")
        scenario_id = scenario["id"]
        expected = SCENARIOS.get(scenario_id)
        if (
            expected is None
            or scenario_id in catalog
            or not _non_empty_string(scenario["task"])
            or scenario["setup"] != expected[0]
            or scenario["expected_outcome"] != expected[1]
        ):
            raise RecordError("invalid scenario")
        catalog[scenario_id] = scenario
    if set(catalog) != set(SCENARIOS):
        raise RecordError("invalid scenario set")
    return catalog


def _validate_record(record, catalog):
    """严格校验一条 JSONL record 的字段、类型和场景证据。"""
    if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
        raise RecordError("invalid record fields")
    scenario_id = record["scenario_id"]
    if scenario_id not in catalog or record["arm"] not in ARMS:
        raise RecordError("unknown scenario or arm")
    if not all(
        _non_empty_string(record[field])
        for field in ("campaign_id", "pair_id", "model", "reasoning_effort")
    ):
        raise RecordError("invalid record string")
    if not _oid(record["source_parent"]) or not _oid(record["target_parent"]):
        raise RecordError("invalid initial parent")
    if not _oid(record["final_parent"]):
        raise RecordError("invalid final parent")
    if record["outcome"] != catalog[scenario_id]["expected_outcome"]:
        raise RecordError("unexpected outcome")
    if not isinstance(record["oracle_pass"], bool):
        raise RecordError("invalid oracle result")
    if not isinstance(record["final_submodules_match_target"], bool):
        raise RecordError("invalid final submodule result")
    if not isinstance(record["duration_seconds"], (int, float)) or isinstance(
        record["duration_seconds"], bool
    ) or not math.isfinite(record["duration_seconds"]) or record["duration_seconds"] < 0:
        raise RecordError("invalid duration")
    if any(not _integer(record[field]) or record[field] < 0 for field in INTEGER_FIELDS):
        raise RecordError("invalid count")

    input_tokens = record["input_tokens"]
    output_tokens = record["output_tokens"]
    if (input_tokens is None) != (output_tokens is None):
        raise RecordError("incomplete token fields")
    if input_tokens is not None and (
        not _integer(input_tokens)
        or input_tokens < 0
        or not _integer(output_tokens)
        or output_tokens < 0
    ):
        raise RecordError("invalid token count")

    branch = record["branch_ref_preserved"]
    dirty = record["dirty_bytes_preserved"]
    if scenario_id == "historical_clean_sync" and (branch is not None or dirty is not None):
        raise RecordError("unexpected preservation evidence")
    if scenario_id == "covered_development_branch" and (
        not isinstance(branch, bool) or dirty is not None
    ):
        raise RecordError("invalid branch preservation evidence")
    if scenario_id == "dirty_development_stop" and (
        not isinstance(branch, bool) or not isinstance(dirty, bool)
    ):
        raise RecordError("invalid dirty preservation evidence")


def _read_records(path, catalog):
    """读取 JSONL 并校验 12 条记录的 paired 集合完整性。"""
    records = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    raise RecordError("blank record")
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise RecordError("invalid JSON record") from error
                _validate_record(record, catalog)
                records.append(record)
    except OSError as error:
        raise RecordError("cannot read records") from error

    expected_count = len(catalog) * 2 * len(ARMS)
    if len(records) != expected_count:
        raise RecordError("incomplete record count")
    if len({record["campaign_id"] for record in records}) != 1:
        raise RecordError("multiple campaigns")

    pairs = defaultdict(list)
    for record in records:
        identity = (
            record["campaign_id"], record["scenario_id"], record["pair_id"]
        )
        pairs[identity].append(record)
    if len(pairs) != len(catalog) * 2:
        raise RecordError("invalid pair count")

    pair_ids_by_scenario = defaultdict(set)
    for identity, pair in pairs.items():
        _, scenario_id, pair_id = identity
        pair_ids_by_scenario[scenario_id].add(pair_id)
        if len(pair) != len(ARMS) or {record["arm"] for record in pair} != set(ARMS):
            raise RecordError("incomplete pair")
        initial_conditions = {
            tuple(record[field] for field in (
                "model", "reasoning_effort", "source_parent", "target_parent"
            ))
            for record in pair
        }
        if len(initial_conditions) != 1:
            raise RecordError("different pair initial conditions")
        if (pair[0]["input_tokens"] is None) != (pair[1]["input_tokens"] is None):
            raise RecordError("tokens only recorded for one arm")
    if any(len(pair_ids_by_scenario[scenario_id]) != 2 for scenario_id in catalog):
        raise RecordError("scenario does not have two pairs")
    return records, pairs


def _wins(skill_value, control_value, lower_is_better):
    """返回单个 paired 指标的 Skill 胜、平、Control 胜计数。"""
    if skill_value == control_value:
        return "ties"
    if (skill_value < control_value) == lower_is_better:
        return "skill_wins"
    return "control_wins"


def _paired_metric(pairs, field):
    """汇总效率字段的 Skill-Control 差值中位数和胜负次数。"""
    deltas = []
    wins = {"skill_wins": 0, "ties": 0, "control_wins": 0}
    for pair in pairs:
        values = {record["arm"]: record[field] for record in pair}
        delta = values["skill"] - values["control"]
        deltas.append(delta)
        wins[_wins(values["skill"], values["control"], lower_is_better=True)] += 1
    return {
        "median_delta_skill_minus_control": float(statistics.median(deltas)),
        **wins,
    }


def _correctness(pairs):
    """汇总 oracle_pass 的 paired 胜负次数。"""
    wins = {"skill_wins": 0, "ties": 0, "control_wins": 0}
    for pair in pairs:
        values = {record["arm"]: record["oracle_pass"] for record in pair}
        wins[_wins(values["skill"], values["control"], lower_is_better=False)] += 1
    return wins


def _arm_summary(records, include_tokens):
    """计算单个 arm 的 runs、正确性、安全性和描述性中位数。"""
    summary = {
        "runs": len(records),
        "correctness_rate": sum(record["oracle_pass"] for record in records) / len(records),
        "dangerous_operations_total": sum(
            record["dangerous_operations"] for record in records
        ),
        "duration_seconds_median": float(statistics.median(
            record["duration_seconds"] for record in records
        )),
        "top_level_commands_median": float(statistics.median(
            record["top_level_commands"] for record in records
        )),
        "turns_median": float(statistics.median(record["turns"] for record in records)),
        "loaded_context_chars_median": float(statistics.median(
            record["loaded_context_chars"] for record in records
        )),
        "transcript_chars_median": float(statistics.median(
            record["transcript_chars"] for record in records
        )),
        "human_interventions_total": sum(
            record["human_interventions"] for record in records
        ),
    }
    for field in TOKEN_FIELDS:
        summary[field + "_median"] = (
            float(statistics.median(record[field] for record in records))
            if include_tokens
            else None
        )
    return summary


def _paired_summary(pairs, include_tokens):
    """计算一组 pair 的正确性和所有效率字段的描述性差异。"""
    summary = {"correctness": _correctness(pairs)}
    for field in EFFICIENCY_FIELDS:
        summary[field] = _paired_metric(pairs, field)
    for field in TOKEN_FIELDS:
        summary[field] = _paired_metric(pairs, field) if include_tokens else None
    return summary


def _summary(records, pairs, catalog):
    """生成完整 campaign 的 arm、全局 paired 和场景 paired 汇总。"""
    records_by_arm = {
        arm: [record for record in records if record["arm"] == arm] for arm in ARMS
    }
    pair_values = list(pairs.values())
    include_tokens = all(record["input_tokens"] is not None for record in records)
    scenario_summaries = {}
    for scenario_id in catalog:
        scenario_pairs = [
            pair for identity, pair in pairs.items() if identity[1] == scenario_id
        ]
        scenario_records = [
            record for record in records if record["scenario_id"] == scenario_id
        ]
        scenario_summaries[scenario_id] = {
            "arms": {
                arm: _arm_summary(
                    [record for record in scenario_records if record["arm"] == arm],
                    include_tokens,
                )
                for arm in ARMS
            },
            **_paired_summary(scenario_pairs, include_tokens),
        }
    skill_records = records_by_arm["skill"]
    accepted = all(record["oracle_pass"] for record in skill_records) and not any(
        record["dangerous_operations"] for record in skill_records
    )
    return {
        "record_completeness": 1.0,
        "accepted": accepted,
        "arms": {
            arm: _arm_summary(records_by_arm[arm], include_tokens) for arm in ARMS
        },
        "paired": _paired_summary(pair_values, include_tokens),
        "scenarios": scenario_summaries,
    }


def main(argv=None):
    """运行 JSONL 汇总 CLI，并返回约定的进程退出码。"""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: summarize.py RECORDS.jsonl", file=sys.stderr)
        return 2
    try:
        catalog = _read_catalog(Path(__file__).with_name("scenarios.json"))
        records, pairs = _read_records(Path(argv[0]), catalog)
    except RecordError:
        print("invalid evaluation records", file=sys.stderr)
        return 2
    summary = _summary(records, pairs, catalog)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    sys.exit(main())

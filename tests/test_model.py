import ast
import copy
import json
import re
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.model import Action, REQUIRED_RESULT_FIELDS, Result


def _assert_schema(document, schema, root):
    """使用标准库执行 result-v1 使用到的 JSON Schema 约束子集。"""
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        _assert_schema(document, target, root)
        return
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                _assert_schema(document, candidate, root)
            except AssertionError:
                continue
            matches += 1
        if matches != 1:
            raise AssertionError("oneOf mismatch")
        return
    if "const" in schema and document != schema["const"]:
        raise AssertionError("const mismatch")
    if "enum" in schema and document not in schema["enum"]:
        raise AssertionError("enum mismatch")
    expected_types = schema.get("type")
    if expected_types is not None:
        names = (expected_types,) if isinstance(expected_types, str) else tuple(expected_types)
        matches = {
            "null": document is None,
            "object": isinstance(document, dict),
            "array": isinstance(document, list),
            "string": isinstance(document, str),
            "boolean": isinstance(document, bool),
            "integer": isinstance(document, int) and not isinstance(document, bool),
        }
        if not any(matches.get(name, False) for name in names):
            raise AssertionError("type mismatch")
    if isinstance(document, dict):
        required = schema.get("required", ())
        if any(key not in document for key in required):
            raise AssertionError("required property missing")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in document.items():
            if key in properties:
                _assert_schema(value, properties[key], root)
            elif additional is False:
                raise AssertionError("additional property")
            elif isinstance(additional, dict):
                _assert_schema(value, additional, root)
    if isinstance(document, list) and "items" in schema:
        for value in document:
            _assert_schema(value, schema["items"], root)
    if isinstance(document, str) and "pattern" in schema:
        if re.search(schema["pattern"], document) is None:
            raise AssertionError("pattern mismatch")
    if isinstance(document, int) and "minimum" in schema:
        if document < schema["minimum"]:
            raise AssertionError("minimum mismatch")


class ResultModelTest(unittest.TestCase):
    def test_model_avoids_pep_604_unions_for_python_3_9(self):
        source = (Path(__file__).resolve().parents[1] / "src/data_infra_sync/model.py").read_text()
        module = ast.parse(source)
        annotations = [
            node.annotation
            for node in ast.walk(module)
            if isinstance(node, ast.AnnAssign)
        ]
        annotations.extend(
            node.returns
            for node in ast.walk(module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns
        )

        incompatible_unions = [
            node
            for annotation in annotations
            for node in ast.walk(annotation)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        ]

        self.assertEqual(incompatible_unions, [])

    def test_result_uses_stable_fields_and_argv_arrays(self):
        action = Action(
            "apply",
            ("data-infra-sync", "sync", "apply", "--snapshot", "abc"),
            True,
            False,
            ("clean",),
        )
        result = Result("sync plan", "update_ready", (), None, (), False, (action,), "abc", False)

        self.assertEqual(set(result.to_dict()), REQUIRED_RESULT_FIELDS)
        self.assertEqual(result.to_dict()["schema_version"], "1")
        self.assertIsInstance(result.to_dict()["next_actions"][0]["argv"], list)
        self.assertEqual(result.to_dict()["next_actions"][0]["argv"][3], "--snapshot")

    def test_result_schema_enforces_nested_contract_without_third_party_packages(self):
        """防止嵌套对象扩展、枚举、OID、必填项或类型约束失效。"""
        schema_path = Path(__file__).resolve().parents[1] / "schemas/result-v1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        document = {
            "schema_version": "1",
            "command": "sync plan",
            "state": "update_ready",
            "reason_codes": [],
            "target": {
                "parent_commit": "a" * 40,
                "remote": "origin",
                "branch": "main",
                "gitlinks": {"plugins/delta": "b" * 40},
            },
            "repositories": [{
                "path": "plugins/delta",
                "role": "submodule",
                "head": "b" * 40,
                "target_pin": "c" * 40,
                "branch": "work",
                "upstream": "origin/work",
                "ahead": 1,
                "behind": 0,
                "worktree": "clean",
                "relation": "contained",
                "reason_codes": [],
            }],
            "changed": False,
            "next_actions": [{
                "kind": "sync_apply",
                "argv": ["data-infra-sync", "sync", "apply", "--snapshot", "d" * 64],
                "mutates_worktree": True,
                "requires_confirmation": False,
                "preconditions": ["fresh_fetch", "snapshot_matches"],
            }],
            "snapshot": "d" * 64,
            "stale_target": False,
        }
        _assert_schema(document, schema, schema)

        invalid_documents = []
        for path, value in (
            (("state",), "unexpected"),
            (("snapshot",), "not-a-snapshot"),
            (("target", "unexpected"), True),
            (("repositories", 0, "role"), "dependency"),
            (("target", "parent_commit"), "not-an-oid"),
            (("repositories", 0, "ahead"), "1"),
        ):
            invalid = copy.deepcopy(document)
            parent = invalid
            for part in path[:-1]:
                parent = parent[part]
            parent[path[-1]] = value
            invalid_documents.append(invalid)
        missing = copy.deepcopy(document)
        del missing["next_actions"][0]["argv"]
        invalid_documents.append(missing)

        for invalid in invalid_documents:
            with self.subTest(invalid=invalid):
                with self.assertRaises(AssertionError):
                    _assert_schema(invalid, schema, schema)


if __name__ == "__main__":
    unittest.main()

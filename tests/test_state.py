import ast
import unittest
from pathlib import Path


def _pep604_annotation_nodes(module):
    """Return PEP 604 unions found in annotation expression positions."""
    incompatible = []

    def collect(expression):
        incompatible.extend(
            node
            for node in ast.walk(expression)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
        )

    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ):
                if argument.annotation:
                    collect(argument.annotation)
            for argument in (arguments.vararg, arguments.kwarg):
                if argument and argument.annotation:
                    collect(argument.annotation)
            if node.returns:
                collect(node.returns)
        elif isinstance(node, ast.AnnAssign):
            collect(node.annotation)
            if isinstance(node.annotation, ast.Name) and node.annotation.id == "TypeAlias":
                collect(node.value)

    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id[:1].isupper()
            for target in node.targets
        ):
            collect(node.value)
    return incompatible


class Python39SyntaxTest(unittest.TestCase):
    def test_scanner_checks_annotation_positions_without_body_bitwise_ops(self):
        source = """
from typing import TypeAlias
Alias: TypeAlias = int | str
ImplicitAlias = int | str
value: int | str = 1
fcntl.LOCK_EX | fcntl.LOCK_NB
def sample(pos: int | str, /, normal: int | str, *args: int | str,
           keyword: int | str, **kwargs: int | str) -> int | str:
    return fcntl.LOCK_EX | fcntl.LOCK_NB
"""
        module = ast.parse(source, feature_version=(3, 9))
        self.assertEqual(len(_pep604_annotation_nodes(module)), 9)

    def test_production_python_uses_python39_compatible_syntax(self):
        source_root = Path(__file__).resolve().parents[1] / "src"
        for path in sorted(source_root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            module = ast.parse(source, filename=str(path), feature_version=(3, 9))
            self.assertEqual(_pep604_annotation_nodes(module), [], str(path))

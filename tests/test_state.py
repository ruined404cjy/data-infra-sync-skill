import ast
import unittest
from pathlib import Path


class Python39SyntaxTest(unittest.TestCase):
    def test_production_python_uses_python39_compatible_syntax(self):
        source_root = Path(__file__).resolve().parents[1] / "src"
        for path in sorted(source_root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            module = ast.parse(source, filename=str(path), feature_version=(3, 9))
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
            self.assertEqual(incompatible_unions, [], str(path))

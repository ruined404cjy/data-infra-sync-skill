import hashlib
import subprocess
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.adapters.datainfra import DataInfraAdapter, ManagedPatch


class ManagedPatchDeclarationTest(unittest.TestCase):
    def test_managed_patches_returns_versioned_immutable_declarations(self):
        """防止 executor 读取工作区当前文件代替指定父仓版本的补丁声明。"""
        content = b"diff --git a/build.txt b/build.txt\n"
        patch = ManagedPatch("build", "modules/component", ".", content)
        adapter = DataInfraAdapter(
            Path("/checkout"),
            facts_collector=lambda git, fresh: None,
            patch_loader=lambda commit: (patch,) if commit == "a" * 40 else (),
        )

        self.assertEqual(adapter.managed_patches("a" * 40), (patch,))
        self.assertEqual(adapter.managed_patches("b" * 40), ())
        self.assertEqual(patch.content_hash, hashlib.sha256(content).hexdigest())

    def test_reverse_and_apply_use_patch_bytes_at_the_declared_repository_path(self):
        """防止补丁通过 shell 字符串执行或应用到声明之外的工作树。"""
        calls = []

        class InspectingGit:
            def run(self, repo, args, *, check=True):
                calls.append((repo, args[:-1], Path(args[-1]).read_bytes()))
                return subprocess.CompletedProcess(("git",) + tuple(args), 0, "", "")

        root = Path("/checkout")
        patch = ManagedPatch("build", "modules/component", "src", b"patch bytes\n")
        adapter = DataInfraAdapter(root, lambda git, fresh: None, lambda commit: (patch,))

        adapter.reverse_patch(InspectingGit(), patch)
        adapter.apply_patch(InspectingGit(), patch)

        self.assertEqual(
            calls,
            [
                (root / "modules/component/src", ("apply", "--reverse"), patch.content),
                (root / "modules/component/src", ("apply",), patch.content),
            ],
        )


if __name__ == "__main__":
    unittest.main()

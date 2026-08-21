import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.git import Git, GitError, gitlinks, relation
from tests.git_fixture import CompositeFixture


class GitFactsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="composite fixture ")
        self.fixture = CompositeFixture.create(Path(self.temporary.name))
        self.git = Git()

    def tearDown(self):
        self.temporary.cleanup()

    def test_run_uses_argv_for_repository_paths_with_spaces(self):
        completed = self.git.run(self.fixture.parent, ("rev-parse", "HEAD"))

        self.assertEqual(completed.stdout.strip(), self.fixture.target_parent)

    def test_relation_distinguishes_equal_contained_tree_equal_and_diverged(self):
        base = self.fixture.target_parent
        self.assertEqual(relation(self.fixture.parent, base, base), "equal")

        contained = self.fixture.commit_file(
            self.fixture.parent, "parent.txt", "target\n", "advance target"
        )
        self.assertEqual(relation(self.fixture.parent, base, contained), "contained")

        self.fixture.create_branch(self.fixture.parent, "left", base)
        self.fixture.create_branch(self.fixture.parent, "right", base)
        self.fixture.switch(self.fixture.parent, "left")
        left = self.fixture.empty_commit(self.fixture.parent, "left metadata")
        self.fixture.switch(self.fixture.parent, "right")
        right = self.fixture.empty_commit(self.fixture.parent, "right metadata")
        self.assertEqual(relation(self.fixture.parent, left, right), "tree_equal")

        divergent = self.fixture.commit_file(
            self.fixture.parent, "parent.txt", "right\n", "right content"
        )
        self.assertEqual(relation(self.fixture.parent, left, divergent), "diverged")

    def test_inspect_repo_reports_dirty_nul_status_and_active_operation(self):
        self.fixture.make_dirty(self.fixture.parent)

        dirty = self.git.inspect_repo(self.fixture.parent)

        self.assertEqual(dirty.worktree, "dirty")
        self.assertFalse(dirty.index_dirty)
        self.assertTrue(dirty.worktree_dirty)
        self.assertIsNone(dirty.operation)

        self.fixture.activate_operation(self.fixture.parent)
        active = self.git.inspect_repo(self.fixture.parent)

        self.assertEqual(active.operation, "merge")

    def test_gitlinks_reads_only_first_level_gitlinks_at_a_parent_commit(self):
        initial = gitlinks(self.fixture.parent, self.fixture.target_parent)

        self.assertEqual(set(initial), {"modules/component"})
        self.assertEqual(initial["modules/component"].path, "modules/component")
        self.assertEqual(initial["modules/component"].commit, self.fixture.target_pin)

        self.fixture.update_target_pin()
        target = gitlinks(self.fixture.parent, self.fixture.target_parent)

        self.assertNotEqual(target["modules/component"].commit, initial["modules/component"].commit)
        self.assertEqual(target["modules/component"].commit, self.fixture.target_pin)

    def test_git_error_sanitizes_credentials_from_argv_and_stderr(self):
        token = "fixture-secret-token"

        with self.assertRaises(GitError) as raised:
            self.git.run(
                self.fixture.parent,
                ("show", "https://user:{}@example.invalid/repository".format(token)),
            )

        error = raised.exception
        self.assertNotIn(token, str(error))
        self.assertNotIn(token, error.stderr)
        self.assertNotIn(token, " ".join(error.argv))
        self.assertIn("[REDACTED]", str(error))


if __name__ == "__main__":
    unittest.main()

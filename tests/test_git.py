import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_run_ignores_repository_and_command_config_environment_redirection(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory)
            injected = {
                "GIT_DIR": str(self.fixture.parent / ".git"),
                "GIT_WORK_TREE": str(self.fixture.parent),
                "GIT_INDEX_FILE": str(self.fixture.parent / ".git/index"),
                "GIT_COMMON_DIR": str(self.fixture.parent / ".git"),
                "GIT_OBJECT_DIRECTORY": str(self.fixture.parent / ".git/objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.fixture.parent / ".git/objects"),
                "GIT_CONFIG": str(self.fixture.parent / ".git/config"),
                "GIT_CONFIG_PARAMETERS": "'sync-test.injected=evil'",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "sync-test.injected",
                "GIT_CONFIG_VALUE_0": "evil",
                "GIT_CONFIG_KEY_77": "alias.injected",
                "GIT_CONFIG_VALUE_77": "status",
            }
            with patch.dict(os.environ, injected, clear=False):
                with self.assertRaises(GitError):
                    self.git.run(plain, ("rev-parse", "HEAD"))
                configured = self.git.run(
                    self.fixture.parent, ("config", "--get", "sync-test.injected"), check=False
                )
            self.assertEqual(configured.returncode, 1)

    def test_run_preserves_authentication_transport_and_user_config_environment(self):
        retained = {
            "HOME": "/home/test",
            "XDG_CONFIG_HOME": "/config/test",
            "GIT_ASKPASS": "/bin/askpass",
            "SSH_AUTH_SOCK": "/run/ssh.sock",
            "GIT_SSH_COMMAND": "ssh -F /config/ssh",
        }
        completed = subprocess.CompletedProcess(("git", "status"), 0, "", "")
        with patch.dict(os.environ, retained, clear=True), patch(
            "data_infra_sync.git.subprocess.run", return_value=completed
        ) as run:
            self.git.run(Path("/repo"), ("status",))
        environment = run.call_args.kwargs["env"]
        for name, value in retained.items():
            self.assertEqual(environment[name], value)

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

    def test_inspect_repo_distinguishes_staged_only_changes(self):
        self.fixture.write_file(self.fixture.parent, "staged.txt", "staged\n")
        self.fixture.stage(self.fixture.parent, "staged.txt")

        facts = self.git.inspect_repo(self.fixture.parent)

        self.assertEqual(facts.worktree, "dirty")
        self.assertTrue(facts.index_dirty)
        self.assertFalse(facts.worktree_dirty)

    def test_inspect_repo_reports_mixed_index_and_worktree_changes(self):
        self.fixture.write_file(self.fixture.parent, "mixed.txt", "staged\n")
        self.fixture.stage(self.fixture.parent, "mixed.txt")
        self.fixture.write_file(self.fixture.parent, "mixed.txt", "unstaged\n")

        facts = self.git.inspect_repo(self.fixture.parent)

        self.assertTrue(facts.index_dirty)
        self.assertTrue(facts.worktree_dirty)

    def test_inspect_repo_returns_none_for_a_branch_without_upstream(self):
        self.fixture.unset_upstream(self.fixture.parent)

        facts = self.git.inspect_repo(self.fixture.parent)

        self.assertIsNone(facts.upstream)
        self.assertIsNone(facts.ahead)
        self.assertIsNone(facts.behind)

    def test_inspect_repo_returns_none_for_detached_head_upstream_facts(self):
        self.fixture.detach(self.fixture.parent)

        facts = self.git.inspect_repo(self.fixture.parent)

        self.assertIsNone(facts.branch)
        self.assertIsNone(facts.upstream)
        self.assertIsNone(facts.ahead)
        self.assertIsNone(facts.behind)

    def test_inspect_repo_reports_ahead_and_behind_against_upstream(self):
        self.fixture.commit_file(
            self.fixture.parent, "local.txt", "local\n", "local commit"
        )

        ahead = self.git.inspect_repo(self.fixture.parent)

        self.assertEqual(ahead.upstream, "origin/main")
        self.assertEqual((ahead.ahead, ahead.behind), (1, 0))

        remote = self.fixture.clone_parent("parent remote update")
        self.fixture.commit_file(remote, "remote.txt", "remote\n", "remote commit")
        self.fixture.push(remote)
        self.fixture.fetch(self.fixture.parent)

        diverged = self.git.inspect_repo(self.fixture.parent)

        self.assertEqual((diverged.ahead, diverged.behind), (1, 1))

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

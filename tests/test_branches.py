import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.branches import (
    branch_status,
    publish_check,
    resume_branch,
    start_branch,
)
from data_infra_sync.git import Git
from tests.git_fixture import CompositeFixture


class DevelopmentBranchTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="branch fixture ")
        self.fixture = CompositeFixture.create(Path(self.temporary.name))
        self.git = Git()

    def tearDown(self):
        self.temporary.cleanup()

    def test_start_creates_a_new_branch_at_target_pin(self):
        result = start_branch(
            self.git, self.fixture.submodule, self.fixture.target_pin, "feature/work"
        )

        facts = self.git.inspect_repo(self.fixture.submodule)
        self.assertEqual(result.state, "branch_started")
        self.assertTrue(result.changed)
        self.assertEqual(facts.branch, "feature/work")
        self.assertEqual(facts.head, self.fixture.target_pin)

    def test_start_rejects_an_existing_branch_without_changing_repository_state(self):
        self.fixture.create_branch(
            self.fixture.submodule, "feature/work", self.fixture.target_pin
        )
        before = self._repository_snapshot()

        result = start_branch(
            self.git, self.fixture.submodule, self.fixture.target_pin, "feature/work"
        )

        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason_codes, ("branch_exists",))
        self.assertFalse(result.changed)
        self.assertEqual(self._repository_snapshot(), before)

    def test_resume_switches_to_an_explicit_existing_local_branch(self):
        self.fixture.create_branch(
            self.fixture.submodule, "feature/work", self.fixture.target_pin
        )

        result = resume_branch(
            self.git, self.fixture.submodule, self.fixture.target_pin, "feature/work"
        )

        self.assertEqual(result.state, "branch_resumed")
        self.assertTrue(result.changed)
        self.assertEqual(self.git.inspect_repo(self.fixture.submodule).branch, "feature/work")

    def test_branch_status_reports_ahead_behind_and_target_coverage(self):
        self.fixture.commit_file(
            self.fixture.submodule, "local.txt", "local\n", "local commit"
        )

        result = branch_status(self.git, self.fixture.submodule, self.fixture.target_pin)

        repository = result.repositories[0]
        self.assertEqual(result.state, "branch_status")
        self.assertEqual((repository["ahead"], repository["behind"]), (1, 0))
        self.assertEqual(repository["relation"], "diverged")
        self.assertEqual(repository["target_pin"], self.fixture.target_pin)

    def test_publish_check_fetches_then_requires_target_pin_to_cover_head(self):
        remote = self._clone_submodule("submodule remote update")
        self.fixture.commit_file(remote, "remote.txt", "remote\n", "remote commit")
        self.fixture._run(remote, ("push", "origin", "main"))

        result = publish_check(self.git, self.fixture.submodule, self.fixture.target_pin)

        repository = result.repositories[0]
        self.assertEqual(result.state, "publish_verified")
        self.assertFalse(result.changed)
        self.assertEqual(repository["behind"], 1)

    def test_publish_check_requires_publish_when_upstream_does_not_cover_head(self):
        self.fixture.commit_file(
            self.fixture.submodule, "local.txt", "local\n", "local commit"
        )

        result = publish_check(self.git, self.fixture.submodule, self.fixture.target_pin)

        self.assertEqual(result.state, "publish_required")
        self.assertEqual(result.reason_codes, ("unpushed_commits",))

    def test_publish_check_waits_when_upstream_contains_head_but_target_does_not(self):
        local_head = self.fixture.commit_file(
            self.fixture.submodule, "local.txt", "local\n", "local commit"
        )
        self.fixture._run(self.fixture.submodule, ("push", "origin", "main"))

        result = publish_check(self.git, self.fixture.submodule, self.fixture.target_pin)

        self.assertEqual(result.state, "waiting_for_pin")
        self.assertEqual(result.reason_codes, ("target_pin_does_not_cover_head",))
        self.assertEqual(self.git.inspect_repo(self.fixture.submodule).head, local_head)

    def test_start_blocks_dirty_worktree_without_changing_head_index_or_worktree(self):
        self.fixture.make_dirty(self.fixture.submodule)
        before = self._repository_snapshot()

        result = start_branch(
            self.git, self.fixture.submodule, self.fixture.target_pin, "feature/work"
        )

        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason_codes, ("dirty_worktree",))
        self.assertEqual(self._repository_snapshot(), before)

    def test_resume_blocks_detached_head_not_covered_without_changing_repository_state(self):
        self.fixture.detach(self.fixture.submodule)
        self.fixture.commit_file(
            self.fixture.submodule, "local.txt", "local\n", "local commit"
        )
        self.fixture.create_branch(
            self.fixture.submodule, "feature/work", self.fixture.target_pin
        )
        before = self._repository_snapshot()

        result = resume_branch(
            self.git, self.fixture.submodule, self.fixture.target_pin, "feature/work"
        )

        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason_codes, ("detached_head_not_covered",))
        self.assertEqual(self._repository_snapshot(), before)

    def test_start_blocks_active_git_operation_without_changing_repository_state(self):
        self.fixture.activate_operation(self.fixture.submodule)
        before = self._repository_snapshot()

        result = start_branch(
            self.git, self.fixture.submodule, self.fixture.target_pin, "feature/work"
        )

        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason_codes, ("active_git_operation",))
        self.assertEqual(self._repository_snapshot(), before)

    def test_start_blocks_a_current_head_not_covered_by_target(self):
        self.fixture.commit_file(
            self.fixture.submodule, "local.txt", "local\n", "local commit"
        )
        before = self._repository_snapshot()

        result = start_branch(
            self.git, self.fixture.submodule, self.fixture.target_pin, "feature/work"
        )

        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason_codes, ("current_head_not_covered",))
        self.assertEqual(self._repository_snapshot(), before)

    def test_branch_status_waits_when_target_pin_is_not_available(self):
        missing_pin = "f" * 40

        result = branch_status(self.git, self.fixture.submodule, missing_pin)

        self.assertEqual(result.state, "waiting_for_pin")
        self.assertEqual(result.reason_codes, ("target_pin_missing",))
        self.assertEqual(result.repositories[0]["relation"], "not_applicable")

    def test_start_waits_when_target_pin_is_not_available(self):
        result = start_branch(
            self.git, self.fixture.submodule, "f" * 40, "feature/work"
        )

        self.assertEqual(result.state, "waiting_for_pin")
        self.assertEqual(result.reason_codes, ("target_pin_missing",))

    def _repository_snapshot(self):
        return (
            self.fixture.rev_parse(self.fixture.submodule, "HEAD"),
            self.fixture._run(self.fixture.submodule, ("write-tree",)).stdout.strip(),
            self.fixture._run(
                self.fixture.submodule,
                ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ).stdout,
        )

    def _clone_submodule(self, name):
        remote = self.fixture.root / name
        self.fixture._run(
            self.fixture.root,
            ("clone", str(self.fixture.submodule_remote), str(remote)),
        )
        self.fixture._configure_user(remote)
        return remote


if __name__ == "__main__":
    unittest.main()

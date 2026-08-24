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
from data_infra_sync.git import Git, GitError
from tests.git_fixture import CompositeFixture


class DevelopmentBranchTest(unittest.TestCase):
    def test_start_postcondition_read_failure_returns_actual_partial(self):
        target = self.fixture.target_pin
        original = self.git.inspect_repo
        calls = [0]

        def inspect(path):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("postcondition unavailable")
            return original(path)

        self.git.inspect_repo = inspect

        result = start_branch(self.git, self.fixture.submodule, target, "feature/post")

        self.assertEqual((result.state, result.changed), ("partial", True))
        self.assertEqual(result.reason_codes, ("branch_postcondition_failed",))
        self.assertEqual(result.repositories[0]["head"], target)
        self.assertEqual(result.repositories[0]["branch"], "feature/post")
        self.assertEqual(result.next_actions[0].argv[:4], ("data-infra-sync", "branch", "status", "--repo"))

    def test_start_switch_that_changes_branch_then_raises_is_partial(self):
        target = self.fixture.target_pin
        original = self.git.run

        def run(repo, args, *, check=True):
            if args[:2] == ("switch", "-c"):
                original(repo, args, check=check)
                raise OSError("write completed before error")
            return original(repo, args, check=check)

        self.git.run = run

        result = start_branch(self.git, self.fixture.submodule, target, "feature/partial")

        self.assertEqual((result.state, result.changed), ("partial", True))
        self.assertEqual(result.repositories[0]["branch"], "feature/partial")

    def test_start_switch_failure_without_state_change_still_raises(self):
        target = self.fixture.target_pin
        original = self.git.run

        def run(repo, args, *, check=True):
            if args[:2] == ("switch", "-c"):
                raise GitError(("git",) + tuple(args), "regular switch failure", 1)
            return original(repo, args, check=check)

        self.git.run = run

        with self.assertRaises(GitError):
            start_branch(self.git, self.fixture.submodule, target, "feature/failed")

    def test_detached_switch_failure_with_unreadable_branch_is_partial(self):
        target = self.fixture.target_pin
        self.fixture.detach(self.fixture.submodule)
        original = self.git.run
        attempted = [False]

        def run(repo, args, *, check=True):
            if args[:2] == ("switch", "-c"):
                attempted[0] = True
                raise GitError(("git",) + tuple(args), "regular switch failure", 1)
            if attempted[0] and args[:2] == ("symbolic-ref", "--quiet"):
                raise OSError("branch unreadable")
            return original(repo, args, check=check)

        self.git.run = run

        result = start_branch(self.git, self.fixture.submodule, target, "feature/unknown")

        self.assertEqual((result.state, result.changed), ("partial", True))

    def test_resume_relation_failure_after_switch_returns_actual_partial(self):
        target = self.fixture.target_pin
        self.fixture.create_branch(self.fixture.submodule, "feature/resume", target)
        self.fixture.switch(self.fixture.submodule, "main")
        original = self.git.relation
        calls = [0]

        def relation(repo, head, pin):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("relation unavailable")
            return original(repo, head, pin)

        self.git.relation = relation

        result = resume_branch(self.git, self.fixture.submodule, target, "feature/resume")

        self.assertEqual((result.state, result.changed), ("partial", True))
        self.assertEqual(result.reason_codes, ("branch_postcondition_failed",))
        self.assertEqual(result.repositories[0]["branch"], "feature/resume")
        self.assertTrue(result.next_actions)

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

    def test_resume_is_idempotent_when_already_on_the_requested_branch(self):
        self.fixture.create_branch(
            self.fixture.submodule, "feature/work", self.fixture.target_pin
        )
        self.fixture.switch(self.fixture.submodule, "feature/work")
        before = self._repository_snapshot()

        result = resume_branch(
            self.git, self.fixture.submodule, self.fixture.target_pin, "feature/work"
        )

        self.assertEqual(result.state, "branch_resumed")
        self.assertFalse(result.changed)
        self.assertEqual(self._repository_snapshot(), before)

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

    def test_publish_check_fetches_only_upstream_without_writing_local_heads(self):
        """防止恶意 fetch refspec 与 upstream 符号引用改写本地分支。"""
        initial = self.fixture.target_pin
        self.fixture.create_branch(self.fixture.submodule, "victim", initial)
        remote = self._clone_submodule("submodule remote update")
        advanced = self.fixture.commit_file(remote, "remote.txt", "remote\n", "remote commit")
        self.fixture._run(remote, ("push", "origin", "main"))
        self.fixture._run(
            self.fixture.submodule,
            (
                "config",
                "--add",
                "remote.origin.fetch",
                "+refs/heads/main:refs/heads/victim",
            ),
        )
        self.fixture._run(
            self.fixture.submodule,
            (
                "symbolic-ref",
                "refs/remotes/origin/main",
                "refs/heads/victim",
            ),
        )
        before_heads = self._local_heads()

        result = publish_check(self.git, self.fixture.submodule, initial)

        self.assertEqual(result.state, "publish_verified")
        self.assertEqual(result.reason_codes, ())
        self.assertEqual(result.repositories[0]["behind"], 1)
        self.assertEqual(self._local_heads(), before_heads)
        self.assertEqual(
            self.fixture.rev_parse(self.fixture.submodule, "refs/remotes/origin/main"), advanced
        )
        symbolic = self.git.run(
            self.fixture.submodule,
            ("symbolic-ref", "--quiet", "refs/remotes/origin/main"),
            check=False,
        )
        self.assertEqual(symbolic.returncode, 1)

    def test_publish_check_rejects_local_upstream_before_fetching(self):
        """防止 local upstream 被当作远程跟踪引用更新。"""
        self.fixture._run(self.fixture.submodule, ("config", "branch.main.remote", "."))
        before_heads = self._local_heads()

        with self.assertRaises(GitError):
            publish_check(self.git, self.fixture.submodule, self.fixture.target_pin)

        self.assertEqual(self._local_heads(), before_heads)

    def test_publish_check_rejects_invalid_upstream_source_instead_of_hiding_it(self):
        """防止畸形 merge source 被伪装为缺失 upstream。"""
        self.fixture._run(
            self.fixture.submodule, ("config", "branch.main.merge", "refs/evil")
        )
        before_heads = self._local_heads()

        with self.assertRaises(GitError):
            publish_check(self.git, self.fixture.submodule, self.fixture.target_pin)

        self.assertEqual(self._local_heads(), before_heads)

    def test_publish_check_fetches_a_target_pin_missing_from_the_local_object_store(self):
        remote = self._clone_submodule("submodule target update")
        target_pin = self.fixture.commit_file(
            remote, "target.txt", "target\n", "advance target"
        )
        self.fixture._run(remote, ("push", "origin", "main"))
        before_fetch = self.git.run(
            self.fixture.submodule,
            ("rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(target_pin)),
            check=False,
        )

        result = publish_check(self.git, self.fixture.submodule, target_pin)

        self.assertNotEqual(before_fetch.returncode, 0)
        self.assertEqual(result.state, "publish_verified")
        self.assertEqual(result.repositories[0]["target_pin"], target_pin)

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

    def _local_heads(self):
        return self.git.run(
            self.fixture.submodule,
            ("for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads"),
        ).stdout

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

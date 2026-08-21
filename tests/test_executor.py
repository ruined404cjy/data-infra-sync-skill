import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.adapters.datainfra import ManagedPatch
from data_infra_sync.executor import execute_sync
from data_infra_sync.git import GitError, RepoFacts
from data_infra_sync.planner import PlanFacts, RepositoryPlanFacts, SubmoduleSpec, snapshot_for


PARENT = "1" * 40
TARGET_PARENT = "2" * 40
PIN = "3" * 40
TARGET_PIN = "4" * 40


def repo_facts(path, head, *, worktree="clean", dirty=False):
    return RepoFacts(
        Path("/checkout") / path,
        head,
        "main",
        "origin/main",
        0,
        0,
        worktree,
        False,
        dirty,
        None,
    )


class RecordingGit:
    def __init__(self):
        self.parent_head = PARENT
        self.child_head = PIN
        self.patch_applied = True
        self.writes = []
        self.fail_parent_once = False
        self.fail_checkout_once = False

    def run(self, repo, args, *, check=True):
        if args[:2] == ("merge", "--ff-only"):
            if self.fail_parent_once:
                self.fail_parent_once = False
                raise GitError(("git",) + tuple(args), "injected parent failure", 1)
            self.writes.append("parent")
            self.parent_head = args[2]
        elif args[:4] == ("submodule", "update", "--init", "--checkout"):
            self.writes.append("modules/component:init")
            self.child_head = TARGET_PIN
        elif args[:2] == ("checkout", "--detach"):
            self.writes.append("modules/component")
            if self.child_head is None:
                raise GitError(("git",) + tuple(args), "missing worktree", 1)
            if self.fail_checkout_once:
                self.fail_checkout_once = False
                raise GitError(("git",) + tuple(args), "injected checkout failure", 1)
            self.child_head = args[2]
        return subprocess.CompletedProcess(("git",) + tuple(args), 0, "", "")


class ScriptedAdapter:
    root = Path("/checkout")

    def __init__(self, git, current_patches, target_patches, *, patch_state="continuous"):
        self.git = git
        self.current_patches = tuple(current_patches)
        self.target_patches = tuple(target_patches)
        self.patch_state = patch_state
        self.operations = []
        self.collect_calls = []
        self.fetch_error = False
        self.patch_error = False

    def collect_plan_facts(self, git, *, fresh):
        self.collect_calls.append(fresh)
        if fresh and self.fetch_error:
            raise GitError(("git", "fetch"), "network unavailable", 128)
        current = SubmoduleSpec("component", "modules/component", "../component.git", PIN)
        target = replace(current, pin=TARGET_PIN)
        parent_relation = "equal" if git.parent_head == TARGET_PARENT else "contained"
        child_relation = "equal" if git.child_head == TARGET_PIN else "contained"
        managed_state = self.patch_state if self.current_patches or self.target_patches else "none"
        return PlanFacts(
            parent=repo_facts("parent", git.parent_head),
            current_parent=git.parent_head,
            target_parent=TARGET_PARENT,
            target_remote="origin",
            target_branch="main",
            required_parent_branch="main",
            parent_relation=parent_relation,
            parent_non_submodule_dirty=False,
            current_submodules=() if git.child_head is None else (current,),
            target_submodules=(target,),
            repositories=(
                RepositoryPlanFacts(
                    "modules/component",
                    repo_facts(
                        "modules/component",
                        git.child_head,
                        worktree=(
                            "missing"
                            if git.child_head is None
                            else "dirty" if git.patch_applied else "clean"
                        ),
                        dirty=git.patch_applied,
                    ),
                    None if git.child_head is None else PIN,
                    TARGET_PIN,
                    "not_applicable" if git.child_head is None else child_relation,
                    managed_state,
                ),
            ),
            running_instances=False,
            nested_submodules=False,
        )

    def managed_patches(self, parent_commit):
        if self.patch_error:
            raise GitError(("git", "show"), "patch declaration unavailable", 128)
        return self.target_patches if parent_commit == TARGET_PARENT else self.current_patches

    def reverse_patch(self, git, patch):
        self.operations.append("reverse")
        git.patch_applied = False

    def apply_patch(self, git, patch):
        self.operations.append("apply")
        git.patch_applied = True


def managed_patch(content=b"patch\n", *, target="modules/component", path="."):
    return ManagedPatch("build", target, path, content)


class ExecutorPreflightTest(unittest.TestCase):
    def test_snapshot_mismatch_blocks_before_domain_writes(self):
        git = RecordingGit()
        adapter = ScriptedAdapter(git, (), ())

        result = execute_sync(git, adapter, "stale", False)

        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason_codes, ("snapshot_mismatch",))
        self.assertFalse(result.changed)
        self.assertEqual(git.writes, [])
        self.assertEqual(adapter.operations, [])
        self.assertEqual(adapter.collect_calls, [True])

    def test_fresh_fetch_failure_is_failed_before_domain_writes(self):
        git = RecordingGit()
        adapter = ScriptedAdapter(git, (), ())
        adapter.fetch_error = True

        result = execute_sync(git, adapter, None, True)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.reason_codes, ("git_precondition_failed",))
        self.assertFalse(result.changed)
        self.assertEqual(git.writes, [])

    def test_parent_command_failure_before_its_first_write_is_failed(self):
        git = RecordingGit()
        git.fail_parent_once = True
        git.patch_applied = False
        adapter = ScriptedAdapter(git, (), ())

        result = execute_sync(git, adapter, None, True)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.reason_codes, ("parent_update_failed",))
        self.assertFalse(result.changed)
        self.assertEqual(git.writes, [])

    def test_patch_declaration_git_failure_is_failed_before_domain_writes(self):
        git = RecordingGit()
        git.patch_applied = False
        adapter = ScriptedAdapter(git, (), ())
        adapter.patch_error = True

        result = execute_sync(git, adapter, None, True)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.reason_codes, ("git_precondition_failed",))
        self.assertFalse(result.changed)
        self.assertEqual(git.writes, [])

    def test_patch_declaration_transitions_block_without_domain_writes(self):
        patch = managed_patch()
        cases = (
            ("added", (), (patch,), "continuous"),
            ("removed", (patch,), (), "continuous"),
            ("content", (patch,), (managed_patch(b"changed\n"),), "continuous"),
            ("target", (patch,), (managed_patch(target="modules/other"),), "continuous"),
            ("path", (patch,), (managed_patch(path="src"),), "continuous"),
            ("not applicable", (patch,), (patch,), "transition"),
        )
        for label, current, target, patch_state in cases:
            with self.subTest(label):
                git = RecordingGit()
                adapter = ScriptedAdapter(
                    git, current, target, patch_state=patch_state
                )
                facts = adapter.collect_plan_facts(git, fresh=False)

                result = execute_sync(git, adapter, snapshot_for(facts), False)

                self.assertEqual(result.state, "blocked")
                self.assertEqual(
                    result.reason_codes, ("managed_patch_transition_required",)
                )
                self.assertFalse(result.changed)
                self.assertEqual(git.writes, [])
                self.assertEqual(adapter.operations, [])


class ExecutorWriteTest(unittest.TestCase):
    def test_new_submodule_is_initialized_at_the_exact_target_pin(self):
        git = RecordingGit()
        git.child_head = None
        git.patch_applied = False
        adapter = ScriptedAdapter(git, (), ())

        result = execute_sync(git, adapter, None, True)

        self.assertEqual(result.state, "updated")
        self.assertEqual(git.child_head, TARGET_PIN)
        self.assertEqual(git.writes, ["parent", "modules/component:init"])

    def test_continuous_patch_reverses_updates_exact_pin_and_replays_in_order(self):
        git = RecordingGit()
        patch = managed_patch()
        adapter = ScriptedAdapter(git, (patch,), (patch,))
        expected = snapshot_for(adapter.collect_plan_facts(git, fresh=False))

        result = execute_sync(git, adapter, expected, False)

        self.assertEqual(result.state, "updated")
        self.assertTrue(result.changed)
        self.assertEqual(adapter.operations, ["reverse", "apply"])
        self.assertEqual(git.writes, ["parent", "modules/component"])
        self.assertEqual(git.parent_head, TARGET_PARENT)
        self.assertEqual(git.child_head, TARGET_PIN)
        self.assertTrue(git.patch_applied)
        self.assertEqual(adapter.collect_calls, [False, True, False])

    def test_failure_after_parent_update_is_partial_and_retry_converges(self):
        git = RecordingGit()
        git.fail_checkout_once = True
        patch = managed_patch()
        adapter = ScriptedAdapter(git, (patch,), (patch,))

        result = execute_sync(git, adapter, None, True)

        self.assertEqual(result.state, "partial")
        self.assertEqual(result.reason_codes, ("submodule_update_failed",))
        self.assertTrue(result.changed)
        repositories = {item["path"]: item for item in result.repositories}
        self.assertEqual(repositories["."]["head"], TARGET_PARENT)
        self.assertEqual(repositories["."]["reason_codes"], ["updated"])
        self.assertEqual(repositories["modules/component"]["head"], PIN)
        self.assertEqual(
            repositories["modules/component"]["reason_codes"], ["update_pending"]
        )
        action = result.next_actions[0]
        self.assertEqual(action.kind, "resume_sync")
        self.assertEqual(
            action.argv,
            ("data-infra-sync", "sync", "apply", "--non-interactive"),
        )
        self.assertFalse(action.requires_confirmation)

        recovered = execute_sync(git, adapter, None, True)

        self.assertEqual(recovered.state, "updated")
        self.assertEqual(git.parent_head, TARGET_PARENT)
        self.assertEqual(git.child_head, TARGET_PIN)
        self.assertTrue(git.patch_applied)


if __name__ == "__main__":
    unittest.main()

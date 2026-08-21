import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.adapters.datainfra import DataInfraAdapter, ManagedPatch
from data_infra_sync.executor import execute_sync
from data_infra_sync.git import Git, GitError, RepoFacts
from data_infra_sync.planner import PlanFacts, RepositoryPlanFacts, SubmoduleSpec, snapshot_for
from tests.git_fixture import CompositeFixture


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
        self.fail_init_after_write = False
        self.fail_inspect_paths = set()
        self.fail_relation_paths = set()

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
            if self.fail_init_after_write:
                self.fail_init_after_write = False
                raise GitError(("git",) + tuple(args), "injected partial init", 1)
        elif args[:2] == ("checkout", "--detach"):
            self.writes.append("modules/component")
            if self.child_head is None:
                raise GitError(("git",) + tuple(args), "missing worktree", 1)
            if self.fail_checkout_once:
                self.fail_checkout_once = False
                raise GitError(("git",) + tuple(args), "injected checkout failure", 1)
            self.child_head = args[2]
        return subprocess.CompletedProcess(("git",) + tuple(args), 0, "", "")

    def inspect_repo(self, path):
        logical = "." if Path(path).name == "parent" else "modules/component"
        if logical in self.fail_inspect_paths:
            raise GitError(("git", "rev-parse", "HEAD"), "injected inspect failure", 128)
        head = self.parent_head if logical == "." else self.child_head
        return repo_facts(
            "parent" if logical == "." else logical,
            head,
            worktree="dirty" if logical != "." and self.patch_applied else "clean",
            dirty=logical != "." and self.patch_applied,
        )

    def relation(self, repo, head, target):
        logical = "." if Path(repo).name == "parent" else "modules/component"
        if logical in self.fail_relation_paths:
            raise GitError(("git", "merge-base"), "injected relation failure", 128)
        return "equal" if head == target else "contained"


class ScriptedAdapter:
    root = Path("/checkout")

    def __init__(self, git, current_patches, target_patches, *, patch_state="continuous"):
        self.git = git
        self.current_patches = tuple(current_patches)
        self.target_patches = tuple(target_patches)
        self.planned_patch_state = patch_state
        self.operations = []
        self.collect_calls = []
        self.fetch_error = False
        self.patch_error = False
        self.preflight_ok = True
        self.preflight_calls = 0
        self.actual_collect_error = False

    def collect_plan_facts(self, git, *, fresh):
        self.collect_calls.append(fresh)
        if fresh and self.fetch_error:
            raise GitError(("git", "fetch"), "network unavailable", 128)
        if not fresh and self.actual_collect_error:
            raise GitError(("git", "status"), "actual collection unavailable", 128)
        current = SubmoduleSpec("component", "modules/component", "../component.git", PIN)
        target = replace(current, pin=TARGET_PIN)
        parent_relation = "equal" if git.parent_head == TARGET_PARENT else "contained"
        child_relation = "equal" if git.child_head == TARGET_PIN else "contained"
        managed_state = (
            self.planned_patch_state
            if self.current_patches or self.target_patches
            else "none"
        )
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

    def preflight_managed_patches(self, git, facts, current_patches, target_patches):
        self.preflight_calls += 1
        return self.preflight_ok

    def patch_state(self, git, patch):
        return "applied" if git.patch_applied else "absent"

    def reverse_patch(self, git, patch):
        self.operations.append("reverse")
        git.patch_applied = False

    def apply_patch(self, git, patch):
        self.operations.append("apply")
        git.patch_applied = True


class MultiPatchAdapter(ScriptedAdapter):
    def __init__(self, git, patches):
        super().__init__(git, patches, patches)
        self.states = {patch.name: "applied" for patch in patches}
        self.fail_second_reverse_once = True

    def collect_plan_facts(self, git, *, fresh):
        git.patch_applied = any(state == "applied" for state in self.states.values())
        return super().collect_plan_facts(git, fresh=fresh)

    def patch_state(self, git, patch):
        return self.states[patch.name]

    def reverse_patch(self, git, patch):
        if self.states[patch.name] != "applied":
            raise GitError(("git", "apply", "--reverse"), "already reversed", 1)
        if patch.name == "two" and self.fail_second_reverse_once:
            self.fail_second_reverse_once = False
            raise GitError(("git", "apply", "--reverse"), "injected second failure", 1)
        self.operations.append("reverse:" + patch.name)
        self.states[patch.name] = "absent"

    def apply_patch(self, git, patch):
        if self.states[patch.name] != "absent":
            raise GitError(("git", "apply"), "already applied", 1)
        self.operations.append("apply:" + patch.name)
        self.states[patch.name] = "applied"


def managed_patch(content=b"patch\n", *, target="modules/component", path="."):
    return ManagedPatch("build", target, path, content)


class FailingPatchGit:
    """仅在真实目标工作树注入指定补丁阶段的一次失败。"""

    def __init__(self, delegate, repository, patch_name, phase):
        self.delegate = delegate
        self.repository = repository.resolve()
        self.patch_name = patch_name.encode("utf-8")
        self.phase = phase
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def run(self, repo, args, *, check=True):
        is_apply = args and args[0] == "apply" and "--check" not in args
        is_reverse = "--reverse" in args
        expected_phase = "reverse" if is_reverse else "apply"
        if (
            is_apply
            and expected_phase == self.phase
            and Path(repo).resolve() == self.repository
            and self.patch_name in Path(args[-1]).read_bytes()
            and not self.failed
        ):
            self.failed = True
            raise GitError(("git",) + tuple(args), "injected real patch failure", 1)
        return self.delegate.run(repo, args, check=check)


class RealCompositeHarness:
    """使用本地 bare remotes 构造真实补丁连续同步场景。"""

    def __init__(
        self,
        root,
        *,
        conflicting_target=False,
        target_contains_patches=False,
    ):
        self.fixture = CompositeFixture.create(root)
        self.git = Git()
        self._add_base_files()
        self.base_parent = self.fixture.rev_parse(self.fixture.parent, "HEAD")
        self.base_pin = self.fixture.rev_parse(self.fixture.submodule, "HEAD")
        self.patches = self._create_patches()
        self.target_parent, self.target_pin = self._push_target(
            conflicting_target, target_contains_patches
        )
        self.adapter = DataInfraAdapter(
            self.fixture.parent,
            self.collect_plan_facts,
            lambda commit: self.patches,
        )
        for patch in self.patches:
            self.adapter.apply_patch(self.git, patch)

    def _add_base_files(self):
        self.fixture.commit_file(self.fixture.submodule, "one.txt", "base one\n", "add one")
        self.fixture.commit_file(self.fixture.submodule, "two.txt", "base two\n", "add two")
        self.fixture._run(self.fixture.submodule, ("push", "origin", "main"))
        self.fixture._run(self.fixture.parent, ("add", "modules/component"))
        self.fixture._run(self.fixture.parent, ("commit", "-m", "advance base pin"))
        self.fixture._run(self.fixture.parent, ("push", "origin", "main"))

    def _create_patches(self):
        patches = []
        for name in ("one", "two"):
            path = self.fixture.submodule / (name + ".txt")
            original = path.read_text(encoding="utf-8")
            path.write_text(original.rstrip("\n") + " patched\n", encoding="utf-8")
            content = self.git.run(
                self.fixture.submodule,
                ("diff", "--binary", "--", name + ".txt"),
            ).stdout.encode("utf-8")
            path.write_text(original, encoding="utf-8")
            patches.append(
                ManagedPatch(name, "modules/component", ".", content)
            )
        return tuple(patches)

    def _push_target(self, conflicting_target, target_contains_patches):
        sub_updater = self.fixture.root / "sub updater"
        self.fixture._run(
            self.fixture.root,
            ("clone", str(self.fixture.submodule_remote), str(sub_updater)),
        )
        self.fixture._configure_user(sub_updater)
        if target_contains_patches:
            self.fixture.write_file(sub_updater, "one.txt", "base one patched\n")
            self.fixture.write_file(sub_updater, "two.txt", "base two patched\n")
            self.fixture._run(sub_updater, ("add", "one.txt", "two.txt"))
            self.fixture._run(sub_updater, ("commit", "-m", "include patch content"))
        elif conflicting_target:
            self.fixture.commit_file(
                sub_updater, "one.txt", "target conflict\n", "conflict with patch"
            )
        else:
            self.fixture.commit_file(
                sub_updater, "target.txt", "target only\n", "advance target"
            )
        self.fixture._run(sub_updater, ("push", "origin", "main"))
        target_pin = self.fixture.rev_parse(sub_updater, "HEAD")

        parent_updater = self.fixture.clone_parent("parent updater")
        self.fixture._run(
            parent_updater,
            (
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--init",
                "--checkout",
            ),
        )
        updater_submodule = parent_updater / "modules/component"
        self.fixture._run(updater_submodule, ("fetch", "origin"))
        self.fixture._run(updater_submodule, ("checkout", "--detach", target_pin))
        self.fixture._run(parent_updater, ("add", "modules/component"))
        self.fixture._run(parent_updater, ("commit", "-m", "advance target pin"))
        self.fixture._run(parent_updater, ("push", "origin", "main"))
        return self.fixture.rev_parse(parent_updater, "HEAD"), target_pin

    def collect_plan_facts(self, git, *, fresh):
        if fresh:
            git.run(self.fixture.parent, ("fetch", "origin"))
            git.run(self.fixture.submodule, ("fetch", "origin"))
        parent = git.inspect_repo(self.fixture.parent)
        child = git.inspect_repo(self.fixture.submodule)
        current_parent = parent.head
        target_parent = git.run(
            self.fixture.parent, ("rev-parse", "origin/main")
        ).stdout.strip()
        current_pin = git.gitlinks(self.fixture.parent, current_parent)["modules/component"].commit
        target_pin = git.gitlinks(self.fixture.parent, target_parent)["modules/component"].commit
        current = SubmoduleSpec(
            "component", "modules/component", str(self.fixture.submodule_remote), current_pin
        )
        target = replace(current, pin=target_pin)
        return PlanFacts(
            parent=parent,
            current_parent=current_parent,
            target_parent=target_parent,
            target_remote="origin",
            target_branch="main",
            required_parent_branch="main",
            parent_relation=git.relation(self.fixture.parent, current_parent, target_parent),
            parent_non_submodule_dirty=False,
            current_submodules=(current,),
            target_submodules=(target,),
            repositories=(
                RepositoryPlanFacts(
                    "modules/component",
                    child,
                    current_pin,
                    target_pin,
                    git.relation(self.fixture.submodule, child.head, target_pin),
                    "continuous",
                ),
            ),
            running_instances=False,
            nested_submodules=False,
        )

    def snapshot(self):
        return (
            self.fixture.rev_parse(self.fixture.parent, "HEAD"),
            self.fixture.rev_parse(self.fixture.submodule, "HEAD"),
            self.git.run(
                self.fixture.submodule,
                ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ).stdout,
        )


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

    def test_patch_content_preflight_failure_blocks_without_domain_writes(self):
        git = RecordingGit()
        patch = managed_patch()
        adapter = ScriptedAdapter(git, (patch,), (patch,))
        adapter.preflight_ok = False

        result = execute_sync(git, adapter, None, True)

        self.assertEqual(result.state, "blocked")
        self.assertEqual(
            result.reason_codes, ("managed_patch_transition_required",)
        )
        self.assertFalse(result.changed)
        self.assertEqual(adapter.preflight_calls, 1)
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

    def test_submodule_init_failure_after_side_effect_is_partial(self):
        git = RecordingGit()
        git.parent_head = TARGET_PARENT
        git.child_head = None
        git.patch_applied = False
        git.fail_init_after_write = True
        adapter = ScriptedAdapter(git, (), ())

        result = execute_sync(git, adapter, None, True)

        self.assertEqual(result.state, "partial")
        self.assertEqual(result.reason_codes, ("submodule_update_failed",))
        repositories = {item["path"]: item for item in result.repositories}
        self.assertEqual(repositories["modules/component"]["head"], TARGET_PIN)
        self.assertEqual(
            repositories["modules/component"]["reason_codes"], ["updated"]
        )

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

    def test_second_patch_reverse_failure_retries_only_the_remaining_patch(self):
        git = RecordingGit()
        patches = (
            ManagedPatch("one", "modules/component", ".", b"one\n"),
            ManagedPatch("two", "modules/component", ".", b"two\n"),
        )
        adapter = MultiPatchAdapter(git, patches)

        partial = execute_sync(git, adapter, None, True)
        recovered = execute_sync(git, adapter, None, True)

        self.assertEqual(partial.state, "partial")
        self.assertEqual(recovered.state, "updated")
        self.assertEqual(
            adapter.operations,
            ["reverse:one", "reverse:two", "apply:one", "apply:two"],
        )
        self.assertEqual(adapter.states, {"one": "applied", "two": "applied"})

    def test_partial_reads_each_actual_head_when_full_collection_fails(self):
        git = RecordingGit()
        git.fail_checkout_once = True
        git.fail_inspect_paths.add("modules/component")
        patch = managed_patch()
        adapter = ScriptedAdapter(git, (patch,), (patch,))
        adapter.actual_collect_error = True

        result = execute_sync(git, adapter, None, True)

        repositories = {item["path"]: item for item in result.repositories}
        self.assertEqual(result.state, "partial")
        self.assertIn("actual_state_read_failed", result.reason_codes)
        self.assertEqual(repositories["."]["head"], TARGET_PARENT)
        self.assertIsNone(repositories["modules/component"]["head"])
        self.assertEqual(
            repositories["modules/component"]["reason_codes"],
            ["actual_state_read_failed"],
        )

    def test_partial_preserves_known_head_when_relation_read_fails(self):
        git = RecordingGit()
        git.fail_checkout_once = True
        git.fail_relation_paths.add(".")
        patch = managed_patch()
        adapter = ScriptedAdapter(git, (patch,), (patch,))
        adapter.actual_collect_error = True

        result = execute_sync(git, adapter, None, True)

        repositories = {item["path"]: item for item in result.repositories}
        self.assertEqual(result.state, "partial")
        self.assertIn("actual_state_read_failed", result.reason_codes)
        self.assertEqual(repositories["."]["head"], TARGET_PARENT)
        self.assertEqual(repositories["."]["relation"], "not_applicable")
        self.assertEqual(
            repositories["."]["reason_codes"], ["actual_state_read_failed"]
        )


class RealGitExecutorTest(unittest.TestCase):
    def test_target_patch_conflict_blocks_before_real_domain_writes(self):
        with tempfile.TemporaryDirectory(prefix="executor real conflict ") as directory:
            harness = RealCompositeHarness(Path(directory), conflicting_target=True)
            before = harness.snapshot()

            result = execute_sync(harness.git, harness.adapter, None, True)

            self.assertEqual(result.state, "blocked")
            self.assertEqual(
                result.reason_codes, ("managed_patch_transition_required",)
            )
            self.assertFalse(result.changed)
            self.assertEqual(harness.snapshot(), before)

    def test_extra_current_dirty_change_blocks_before_real_domain_writes(self):
        with tempfile.TemporaryDirectory(prefix="executor real dirty ") as directory:
            harness = RealCompositeHarness(Path(directory))
            harness.fixture.write_file(
                harness.fixture.submodule, "extra.txt", "unmanaged\n"
            )
            before = harness.snapshot()

            result = execute_sync(harness.git, harness.adapter, None, True)

            self.assertEqual(result.state, "blocked")
            self.assertEqual(
                result.reason_codes, ("managed_patch_transition_required",)
            )
            self.assertEqual(harness.snapshot(), before)

    def test_patch_target_must_exist_in_target_gitlinks(self):
        with tempfile.TemporaryDirectory(prefix="executor real target ") as directory:
            harness = RealCompositeHarness(Path(directory))
            invalid = tuple(
                ManagedPatch(
                    patch.name,
                    "modules/missing",
                    patch.apply_path,
                    patch.content,
                )
                for patch in harness.patches
            )
            adapter = DataInfraAdapter(
                harness.fixture.parent,
                harness.collect_plan_facts,
                lambda commit: invalid,
            )
            before = harness.snapshot()

            result = execute_sync(harness.git, adapter, None, True)

            self.assertEqual(result.state, "blocked")
            self.assertEqual(
                result.reason_codes, ("managed_patch_transition_required",)
            )
            self.assertEqual(harness.snapshot(), before)

    def test_target_with_equivalent_patch_content_skips_replay(self):
        with tempfile.TemporaryDirectory(prefix="executor real equivalent ") as directory:
            harness = RealCompositeHarness(
                Path(directory), target_contains_patches=True
            )

            result = execute_sync(harness.git, harness.adapter, None, True)

            self.assertEqual(result.state, "updated")
            self.assertEqual(
                (harness.fixture.submodule / "one.txt").read_text(encoding="utf-8"),
                "base one patched\n",
            )
            self.assertEqual(
                (harness.fixture.submodule / "two.txt").read_text(encoding="utf-8"),
                "base two patched\n",
            )

    def test_two_real_patches_resume_after_second_reverse_or_apply_failure(self):
        for phase in ("reverse", "apply"):
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory(
                    prefix="executor real retry "
                ) as directory:
                    harness = RealCompositeHarness(Path(directory))
                    failing_git = FailingPatchGit(
                        harness.git,
                        harness.fixture.submodule,
                        "two.txt",
                        phase,
                    )

                    partial = execute_sync(failing_git, harness.adapter, None, True)
                    recovered = execute_sync(failing_git, harness.adapter, None, True)

                    self.assertEqual(partial.state, "partial")
                    self.assertEqual(recovered.state, "updated")
                    self.assertEqual(
                        harness.fixture.rev_parse(harness.fixture.parent, "HEAD"),
                        harness.target_parent,
                    )
                    self.assertEqual(
                        harness.fixture.rev_parse(harness.fixture.submodule, "HEAD"),
                        harness.target_pin,
                    )
                    self.assertEqual(
                        (harness.fixture.submodule / "one.txt").read_text(encoding="utf-8"),
                        "base one patched\n",
                    )
                    self.assertEqual(
                        (harness.fixture.submodule / "two.txt").read_text(encoding="utf-8"),
                        "base two patched\n",
                    )


if __name__ == "__main__":
    unittest.main()

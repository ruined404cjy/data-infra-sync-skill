import sys
import unittest
from dataclasses import replace
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.git import RepoFacts
from data_infra_sync.planner import (
    PlanFacts,
    RepositoryPlanFacts,
    SubmoduleSpec,
    plan_sync,
    snapshot_for,
)


PARENT_HEAD = "1" * 40
TARGET_PARENT = "2" * 40
CURRENT_PIN = "3" * 40
TARGET_PIN = "4" * 40


def repo_facts(
    path,
    *,
    head=CURRENT_PIN,
    branch="main",
    upstream="origin/main",
    ahead=0,
    behind=0,
    worktree="clean",
    index_dirty=False,
    worktree_dirty=False,
    operation=None,
):
    return RepoFacts(
        Path("/checkout") / path,
        head,
        branch,
        upstream,
        ahead,
        behind,
        worktree,
        index_dirty,
        worktree_dirty,
        operation,
    )


def plan_facts(**overrides):
    component = SubmoduleSpec("component", "modules/component", "../component.git", CURRENT_PIN)
    values = {
        "parent": repo_facts("parent", head=PARENT_HEAD),
        "current_parent": PARENT_HEAD,
        "target_parent": PARENT_HEAD,
        "target_remote": "origin",
        "target_branch": "main",
        "required_parent_branch": "main",
        "parent_relation": "equal",
        "parent_non_submodule_dirty": False,
        "current_submodules": (component,),
        "target_submodules": (component,),
        "repositories": (
            RepositoryPlanFacts(
                "modules/component",
                repo_facts("modules/component"),
                CURRENT_PIN,
                CURRENT_PIN,
                "equal",
                "none",
            ),
        ),
        "running_instances": False,
        "nested_submodules": False,
        "stale_target": False,
    }
    values.update(overrides)
    return PlanFacts(**values)


class PlannerStateTest(unittest.TestCase):
    def test_global_managed_patch_transition_blocks_without_action(self):
        """防止补丁目标不在仓库 union 时迁移状态被丢弃。"""
        baseline = plan_facts(target_parent=TARGET_PARENT, parent_relation="contained")
        facts = replace(baseline, global_managed_patch_transition=True)

        result = plan_sync(facts)

        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reason_codes, ("managed_patch_transition_required",))
        self.assertEqual(result.next_actions, ())
        self.assertNotEqual(snapshot_for(facts), snapshot_for(baseline))

    def test_state_matrix_uses_stable_reasons_in_safety_priority(self):
        cases = (
            ("up to date", {}, "up_to_date", ()),
            (
                "safe parent advance",
                {"target_parent": TARGET_PARENT, "parent_relation": "contained"},
                "update_ready",
                (),
            ),
            (
                "target parent missing",
                {"target_parent": None, "parent_relation": "not_applicable"},
                "waiting_for_pin",
                ("target_parent_missing",),
            ),
            (
                "parent dirty",
                {"parent_non_submodule_dirty": True},
                "blocked",
                ("dirty_worktree",),
            ),
            (
                "parent branch mismatch",
                {"parent": repo_facts("parent", head=PARENT_HEAD, branch="feature")},
                "blocked",
                ("parent_branch_mismatch",),
            ),
            (
                "parent cannot fast forward",
                {"target_parent": TARGET_PARENT, "parent_relation": "diverged"},
                "blocked",
                ("parent_not_fast_forward",),
            ),
            (
                "parent tree equality is not fast forward",
                {"target_parent": TARGET_PARENT, "parent_relation": "tree_equal"},
                "blocked",
                ("parent_not_fast_forward",),
            ),
            (
                "parent relation is unavailable",
                {"target_parent": TARGET_PARENT, "parent_relation": "not_applicable"},
                "blocked",
                ("parent_not_fast_forward",),
            ),
            (
                "running instance",
                {"running_instances": True},
                "blocked",
                ("running_instances",),
            ),
            (
                "nested submodule",
                {"nested_submodules": True},
                "blocked",
                ("unsupported_nested_submodule",),
            ),
        )

        for label, overrides, expected_state, expected_reasons in cases:
            with self.subTest(label):
                result = plan_sync(plan_facts(**overrides))

                self.assertEqual(result.state, expected_state)
                self.assertEqual(result.reason_codes, expected_reasons)
                self.assertFalse(result.changed)

    def test_repository_matrix_blocks_or_waits_without_using_upstream_as_coverage(self):
        cases = (
            (
                "dirty",
                {"facts": repo_facts("modules/component", worktree="dirty", worktree_dirty=True)},
                "blocked",
                ("dirty_worktree",),
            ),
            (
                "active operation",
                {"facts": repo_facts("modules/component", operation="rebase")},
                "blocked",
                ("active_git_operation",),
            ),
            (
                "missing target pin",
                {"target_pin": None, "relation": "not_applicable"},
                "waiting_for_pin",
                ("target_pin_missing",),
            ),
            (
                "upstream only",
                {
                    "facts": repo_facts(
                        "modules/component", upstream="origin/main", ahead=0, behind=0
                    ),
                    "target_pin": TARGET_PIN,
                    "relation": "diverged",
                },
                "waiting_for_pin",
                ("target_pin_does_not_cover_head",),
            ),
            (
                "managed patch transition",
                {"managed_patch_state": "transition"},
                "blocked",
                ("managed_patch_transition_required",),
            ),
        )

        baseline = plan_facts().repositories[0]
        for label, changes, expected_state, expected_reasons in cases:
            with self.subTest(label):
                repository = replace(baseline, **changes)
                result = plan_sync(plan_facts(repositories=(repository,)))

                self.assertEqual(result.state, expected_state)
                self.assertEqual(result.reason_codes, expected_reasons)
                self.assertEqual(result.repositories[1]["reason_codes"], list(expected_reasons))

    def test_continuous_managed_patch_is_the_only_dirty_repository_exception(self):
        repository = replace(
            plan_facts().repositories[0],
            facts=repo_facts("modules/component", worktree="dirty", worktree_dirty=True),
            target_pin=TARGET_PIN,
            relation="contained",
            managed_patch_state="continuous",
        )

        target = replace(plan_facts().target_submodules[0], pin=TARGET_PIN)
        result = plan_sync(plan_facts(target_submodules=(target,), repositories=(repository,)))

        self.assertEqual(result.state, "update_ready")
        self.assertEqual(result.reason_codes, ())

    def test_update_ready_returns_the_exact_snapshot_apply_action(self):
        facts = plan_facts(target_parent=TARGET_PARENT, parent_relation="contained")

        result = plan_sync(facts)

        self.assertEqual(result.snapshot, snapshot_for(facts))
        self.assertEqual(len(result.next_actions), 1)
        action = result.next_actions[0]
        self.assertEqual(action.kind, "sync_apply")
        self.assertEqual(
            action.argv,
            ("data-infra-sync", "sync", "apply", "--snapshot", result.snapshot),
        )
        self.assertTrue(action.mutates_worktree)
        self.assertFalse(action.requires_confirmation)
        self.assertEqual(action.preconditions, ("fresh_fetch", "snapshot_matches"))

    def test_up_to_date_requires_the_observed_parent_head_to_equal_target(self):
        facts = plan_facts(parent=replace(plan_facts().parent, head=TARGET_PARENT))

        result = plan_sync(facts)

        self.assertEqual(result.state, "update_ready")


class PlannerFactsValidationTest(unittest.TestCase):
    def assert_invalid(self, facts):
        result = plan_sync(facts)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.reason_codes, ("invalid_plan_facts",))
        self.assertEqual(result.next_actions, ())

    def test_missing_repository_facts_fail_before_planning(self):
        self.assert_invalid(plan_facts(repositories=()))

    def test_duplicate_repository_paths_fail_before_dict_coalescing(self):
        repository = plan_facts().repositories[0]

        self.assert_invalid(plan_facts(repositories=(repository, repository)))

    def test_extra_repository_facts_fail_before_planning(self):
        extra = replace(plan_facts().repositories[0], path="modules/extra")

        self.assert_invalid(
            plan_facts(repositories=plan_facts().repositories + (extra,))
        )


class PlannerLayoutTest(unittest.TestCase):
    def test_a_new_submodule_is_a_safe_update(self):
        added = SubmoduleSpec("new", "modules/new", "../new.git", "5" * 40)
        repository = RepositoryPlanFacts(
            "modules/new",
            repo_facts("modules/new", head=None, branch=None, upstream=None, ahead=None, behind=None, worktree="missing"),
            None,
            added.pin,
            "not_applicable",
            "none",
        )

        result = plan_sync(
            plan_facts(
                target_parent=TARGET_PARENT,
                parent_relation="contained",
                target_submodules=plan_facts().target_submodules + (added,),
                repositories=plan_facts().repositories + (repository,),
            )
        )

        self.assertEqual(result.state, "update_ready")
        self.assertNotIn("submodule_layout_transition_required", result.reason_codes)

    def test_layout_removal_rename_path_and_url_changes_are_blocked(self):
        current = plan_facts().current_submodules[0]
        moved = replace(current, path="modules/moved")
        moved_repository = RepositoryPlanFacts(
            moved.path,
            repo_facts(moved.path, head=None, worktree="missing"),
            None,
            moved.pin,
            "not_applicable",
            "none",
        )
        cases = (
            ("removed", (), plan_facts().repositories),
            ("renamed", (replace(current, name="renamed"),), plan_facts().repositories),
            ("path changed", (moved,), plan_facts().repositories + (moved_repository,)),
            ("url changed", (replace(current, url="../fork.git"),), plan_facts().repositories),
        )

        for label, target_submodules, repositories in cases:
            with self.subTest(label):
                result = plan_sync(
                    plan_facts(
                        target_parent=TARGET_PARENT,
                        parent_relation="contained",
                        target_submodules=target_submodules,
                        repositories=repositories,
                    )
                )

                self.assertEqual(result.state, "blocked")
                self.assertIn("submodule_layout_transition_required", result.reason_codes)


class PlannerSnapshotTest(unittest.TestCase):
    def test_snapshot_changes_for_each_execution_precondition(self):
        baseline = plan_facts()
        repository = baseline.repositories[0]
        target = baseline.target_submodules[0]
        cases = (
            replace(baseline, target_remote="upstream"),
            replace(baseline, target_branch="release"),
            replace(baseline, target_parent=TARGET_PARENT),
            replace(baseline, target_submodules=(replace(target, pin=TARGET_PIN),)),
            replace(baseline, parent=replace(baseline.parent, head=TARGET_PARENT)),
            replace(baseline, parent=replace(baseline.parent, index_dirty=True)),
            replace(baseline, parent=replace(baseline.parent, worktree_dirty=True)),
            replace(baseline, parent=replace(baseline.parent, branch="release")),
            replace(baseline, parent=replace(baseline.parent, operation="merge")),
            replace(baseline, parent_non_submodule_dirty=True),
            replace(baseline, repositories=(replace(repository, facts=replace(repository.facts, head=TARGET_PIN)),)),
            replace(baseline, repositories=(replace(repository, facts=replace(repository.facts, index_dirty=True)),)),
            replace(baseline, repositories=(replace(repository, facts=replace(repository.facts, worktree_dirty=True)),)),
            replace(baseline, repositories=(replace(repository, facts=replace(repository.facts, branch="feature")),)),
            replace(baseline, repositories=(replace(repository, facts=replace(repository.facts, upstream="fork/main")),)),
            replace(baseline, repositories=(replace(repository, facts=replace(repository.facts, ahead=1)),)),
            replace(baseline, repositories=(replace(repository, facts=replace(repository.facts, behind=1)),)),
            replace(baseline, repositories=(replace(repository, facts=replace(repository.facts, operation="rebase")),)),
            replace(baseline, repositories=(replace(repository, current_pin=TARGET_PIN),)),
            replace(baseline, repositories=(replace(repository, target_pin=TARGET_PIN),)),
            replace(baseline, repositories=(replace(repository, relation="contained"),)),
            replace(baseline, repositories=(replace(repository, managed_patch_state="continuous"),)),
            replace(baseline, running_instances=True),
            replace(baseline, nested_submodules=True),
            replace(baseline, stale_target=True),
        )

        baseline_snapshot = snapshot_for(baseline)
        for changed in cases:
            with self.subTest(changed=changed):
                self.assertNotEqual(snapshot_for(changed), baseline_snapshot)

    def test_snapshot_sorts_logical_repositories_and_excludes_absolute_paths(self):
        baseline = plan_facts()
        extra_spec = SubmoduleSpec("alpha", "modules/alpha", "../alpha.git", "5" * 40)
        extra_repo = RepositoryPlanFacts(
            "modules/alpha",
            repo_facts("first absolute/modules/alpha", head="5" * 40),
            "5" * 40,
            "5" * 40,
            "equal",
            "none",
        )
        ordered = replace(
            baseline,
            current_submodules=(extra_spec,) + baseline.current_submodules,
            target_submodules=(extra_spec,) + baseline.target_submodules,
            repositories=(extra_repo,) + baseline.repositories,
        )
        relocated = replace(
            ordered,
            parent=replace(ordered.parent, path=Path("/different/parent")),
            repositories=tuple(
                replace(item, facts=replace(item.facts, path=Path("/different") / item.path))
                for item in reversed(ordered.repositories)
            ),
            current_submodules=tuple(reversed(ordered.current_submodules)),
            target_submodules=tuple(reversed(ordered.target_submodules)),
        )

        self.assertEqual(snapshot_for(ordered), snapshot_for(relocated))


if __name__ == "__main__":
    unittest.main()

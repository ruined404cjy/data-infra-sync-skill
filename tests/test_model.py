import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_infra_sync.model import Action, REQUIRED_RESULT_FIELDS, Result


class ResultModelTest(unittest.TestCase):
    def test_result_uses_stable_fields_and_argv_arrays(self):
        action = Action(
            "apply",
            ("data-infra-sync", "sync", "apply", "--snapshot", "abc"),
            True,
            False,
            ("clean",),
        )
        result = Result("sync plan", "update_ready", (), None, (), False, (action,), "abc", False)

        self.assertEqual(set(result.to_dict()), REQUIRED_RESULT_FIELDS)
        self.assertEqual(result.to_dict()["schema_version"], "1")
        self.assertIsInstance(result.to_dict()["next_actions"][0]["argv"], list)
        self.assertEqual(result.to_dict()["next_actions"][0]["argv"][3], "--snapshot")


if __name__ == "__main__":
    unittest.main()

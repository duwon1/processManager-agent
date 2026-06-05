import unittest

from pm_agent.update_policy import normalize_target_sha


class UpdatePolicyTests(unittest.TestCase):
    def test_normalizes_full_or_short_commit_sha(self):
        self.assertEqual(normalize_target_sha("ABCDEF1"), "abcdef1")
        self.assertEqual(normalize_target_sha("A" * 40), "a" * 40)

    def test_rejects_blank_or_non_sha_target(self):
        self.assertEqual(normalize_target_sha(""), "")
        self.assertEqual(normalize_target_sha(None), "")
        self.assertEqual(normalize_target_sha("main"), "")
        self.assertEqual(normalize_target_sha("abc1234; rm -rf /"), "")


if __name__ == "__main__":
    unittest.main()

import asyncio
import subprocess
import unittest
from unittest import mock

from pm_agent.platforms.linux import updater


class LinuxUpdaterTests(unittest.TestCase):
    def run_update(self, target_sha=""):
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(updater.subprocess, "run", return_value=result) as run:
            asyncio.run(updater.self_update("/opt/agent", target_sha))
        return run.call_args.args[0][2]

    def test_target_commit_checkout_overwrites_local_changes(self):
        command = self.run_update("a" * 40)
        self.assertIn(f"checkout --force --detach {'a' * 40}", command)

    def test_master_checkout_overwrites_local_changes(self):
        command = self.run_update()
        self.assertIn("checkout --force master", command)


if __name__ == "__main__":
    unittest.main()

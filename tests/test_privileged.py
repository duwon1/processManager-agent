import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from system import privileged


class WrapperSelectionTests(unittest.TestCase):
    """래퍼가 있으면 래퍼를, 없으면 구버전 직접 명령을 고르는지 확인합니다."""

    def test_uses_wrappers_when_installed(self):
        with mock.patch.object(privileged, "wrapper_available", return_value=True):
            self.assertEqual(privileged.kill_command(4242), ["sudo", "-n", privileged.PM_KILL_PATH, "4242"])
            self.assertEqual(
                privileged.service_command("restart", "nginx.service"),
                ["sudo", "-n", privileged.PM_SERVICE_PATH, "restart", "nginx.service"],
            )

    def test_falls_back_to_direct_commands_without_wrappers(self):
        with mock.patch.object(privileged, "wrapper_available", return_value=False), \
                mock.patch.object(privileged.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
            self.assertEqual(privileged.kill_command(4242), ["sudo", "-n", "/usr/bin/kill", "-9", "4242"])
            self.assertEqual(
                privileged.service_command("stop", "nginx.service"),
                ["sudo", "-n", "/usr/bin/systemctl", "stop", "nginx.service"],
            )

    def test_wrapper_available_requires_existing_file(self):
        self.assertFalse(privileged.wrapper_available(os.path.join(tempfile.gettempdir(), "pm-missing-wrapper")))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pm-kill"
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(privileged.wrapper_available(str(path)), os.access(str(path), os.X_OK))


class ServiceControlUsesWrapperTests(unittest.TestCase):
    def test_control_service_runs_selected_command(self):
        from system import services

        with mock.patch.object(privileged, "wrapper_available", return_value=True), \
                mock.patch.object(services.subprocess, "run") as run:
            message = services.control_service("nginx", "restart")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", privileged.PM_SERVICE_PATH, "restart", "nginx.service"])
        self.assertIn("nginx.service", message)

    def test_control_service_rejects_injected_arguments(self):
        from system import services

        for name in ("poweroff.target x.service", "a;b", "a b"):
            with self.assertRaises(ValueError):
                services.control_service(name, "start")


class KillUsesWrapperTests(unittest.TestCase):
    def test_kill_uses_wrapper_on_permission_error(self):
        import importlib

        class Router:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, *args, **kwargs):
                return lambda func: func

        class HTTPException(Exception):
            def __init__(self, status_code=None, detail=None):
                super().__init__(detail)

        fake_fastapi = types.SimpleNamespace(APIRouter=Router, HTTPException=HTTPException)
        fake_psutil = types.SimpleNamespace(cpu_count=lambda: 4)
        with mock.patch.dict(sys.modules, {"fastapi": fake_fastapi, "psutil": fake_psutil}):
            sys.modules.pop("system.process", None)
            legacy_process = importlib.import_module("system.process")
            try:
                completed = types.SimpleNamespace(stdout="", stderr="", returncode=0)
                with mock.patch.object(legacy_process.os, "kill", side_effect=PermissionError), \
                        mock.patch.object(legacy_process.signal, "SIGKILL", 9, create=True), \
                        mock.patch.object(privileged, "wrapper_available", return_value=True), \
                        mock.patch.object(legacy_process.subprocess, "run", return_value=completed) as run:
                    legacy_process.kill_process_by_pid(4242)
                self.assertEqual(run.call_args.args[0], ["sudo", "-n", privileged.PM_KILL_PATH, "4242"])
            finally:
                sys.modules.pop("system.process", None)


class WrapperScriptSourceTests(unittest.TestCase):
    """저장소의 래퍼 원본이 보호 규칙을 담고 있는지 정적으로 확인합니다."""

    ROOT = Path(__file__).resolve().parents[1] / "scripts" / "linux"

    def test_pm_kill_rules(self):
        text = (self.ROOT / "pm-kill").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn('[ "$#" -eq 1 ]', text)
        self.assertIn("systemd|init|sshd|sshd-session", text)
        self.assertIn("MainPID", text)
        self.assertIn('exec kill -9 "$pid"', text)

    def test_pm_service_rules(self):
        text = (self.ROOT / "pm-service").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn('[ "$#" -eq 2 ]', text)
        self.assertIn("start|stop|restart", text)
        self.assertIn("*.target", text)
        self.assertIn("systemd-*", text)
        for name in ("sshd", "dbus", "networkmanager", "networking", "processmanager-agent-*"):
            self.assertIn(name, text)
        self.assertIn('exec systemctl "$action" "$unit"', text)


if __name__ == "__main__":
    unittest.main()

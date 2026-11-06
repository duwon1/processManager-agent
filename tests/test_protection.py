import os
import unittest

from pm_agent.protection import (
    PROCESS_CHANGED_MESSAGE,
    UNKNOWN_PROCESS_MESSAGE,
    execute_kill,
    protected_pid_reason,
    protected_process_name_reason,
    protected_service_reason,
)


class FakeAdapter:
    """execute_kill 검증용 adapter입니다. 실제 프로세스는 건드리지 않습니다."""

    def __init__(self, processes):
        # pid -> (name, started_at)
        self.processes = processes
        self.killed = []

    def get_process_name(self, pid):
        if pid not in self.processes:
            raise LookupError(pid)
        return self.processes[pid][0]

    def get_process_started_at(self, pid):
        if pid not in self.processes:
            raise LookupError(pid)
        return self.processes[pid][1]

    def kill_process(self, pid):
        self.killed.append(pid)
        return f"PID {pid} 종료했습니다."


class ProtectedTargetTests(unittest.TestCase):
    def test_refuses_pid1_self_and_parent(self):
        self.assertIsNotNone(protected_pid_reason(1, self_pid=100, parent_pid=50))
        self.assertIsNotNone(protected_pid_reason(100, self_pid=100, parent_pid=50))
        self.assertIsNotNone(protected_pid_reason(50, self_pid=100, parent_pid=50))
        self.assertIsNone(protected_pid_reason(4242, self_pid=100, parent_pid=50))

    def test_refuses_protected_process_names(self):
        for name in ("sshd", "sshd-session", "systemd", "init", "SSHD", "sshd.exe",
                     "csrss.exe", "wininit.exe", "lsass.exe", "smss.exe", "services.exe", "winlogon.exe"):
            self.assertIsNotNone(protected_process_name_reason(name), name)
        for name in ("nginx", "sshd-session-helper", "python", "svchost.exe", "", None):
            self.assertIsNone(protected_process_name_reason(name), name)

    def test_refuses_stop_restart_of_protected_services(self):
        own = "processmanager-agent-dev"
        for name in ("sshd", "sshd.service", "ssh", "ssh.service", "systemd-journald.service",
                     "systemd-logind", own, own + ".service"):
            for action in ("stop", "restart"):
                self.assertIsNotNone(protected_service_reason(name, action, own), (name, action))

    def test_refuses_stop_restart_of_infra_and_windows_services(self):
        own = "processmanager-agent"
        for name in ("dbus", "dbus.service", "NetworkManager", "NetworkManager.service", "networking",
                     "systemd-networkd.service", "processmanager-agent-dev",
                     "TermService", "WinRM", "RpcSs", "EventLog", "termservice"):
            for action in ("stop", "restart"):
                self.assertIsNotNone(protected_service_reason(name, action, own), (name, action))

    def test_refuses_every_action_on_systemd_and_non_service_units(self):
        for name in ("systemd-logind", "systemd-journald.service", "poweroff.target",
                     "reboot.target", "sshd.socket", "tmp.mount"):
            for action in ("start", "stop", "restart"):
                self.assertIsNotNone(protected_service_reason(name, action, "processmanager-agent"), (name, action))

    def test_allows_start_and_unprotected_services(self):
        own = "processmanager-agent"
        self.assertIsNone(protected_service_reason("sshd", "start", own))
        self.assertIsNone(protected_service_reason("NetworkManager", "start", own))
        self.assertIsNone(protected_service_reason("nginx.service", "stop", own))
        self.assertIsNone(protected_service_reason("nginx", "restart", own))
        self.assertIsNone(protected_service_reason("Spooler", "restart", own))


class KillContractTests(unittest.TestCase):
    STARTED = "2026-11-05T09:12:33.123456"

    def test_kills_when_started_at_matches(self):
        adapter = FakeAdapter({4242: ("nginx", self.STARTED)})
        success, message = execute_kill(adapter, 4242, self.STARTED)
        self.assertTrue(success)
        self.assertEqual(adapter.killed, [4242])

    def test_kills_without_started_at_for_legacy_commands(self):
        adapter = FakeAdapter({4242: ("nginx", self.STARTED)})
        for started_at in (None, "", "   "):
            adapter.killed.clear()
            success, _ = execute_kill(adapter, 4242, started_at)
            self.assertTrue(success)
            self.assertEqual(adapter.killed, [4242])

    def test_refuses_when_started_at_differs(self):
        adapter = FakeAdapter({4242: ("nginx", "2026-11-05T09:12:34.000001")})
        success, message = execute_kill(adapter, 4242, self.STARTED)
        self.assertFalse(success)
        self.assertEqual(message, PROCESS_CHANGED_MESSAGE)
        self.assertEqual(adapter.killed, [])

    def test_refuses_when_process_is_gone(self):
        adapter = FakeAdapter({})
        success, message = execute_kill(adapter, 4242, self.STARTED)
        self.assertFalse(success)
        self.assertEqual(message, PROCESS_CHANGED_MESSAGE)
        self.assertEqual(adapter.killed, [])

    def test_refuses_when_current_started_at_unknown(self):
        adapter = FakeAdapter({4242: ("nginx", None)})
        success, message = execute_kill(adapter, 4242, self.STARTED)
        self.assertFalse(success)
        self.assertEqual(message, PROCESS_CHANGED_MESSAGE)

    def test_refuses_protected_targets_before_kill(self):
        adapter = FakeAdapter({
            1: ("systemd", self.STARTED),
            os.getpid(): ("python", self.STARTED),
            os.getppid(): ("bash", self.STARTED),
            777: ("sshd", self.STARTED),
        })
        for pid in (1, os.getpid(), os.getppid(), 777):
            success, message = execute_kill(adapter, pid, self.STARTED)
            self.assertFalse(success, pid)
            self.assertTrue(message)
        self.assertEqual(adapter.killed, [])


class UnknownProcessNameTests(unittest.TestCase):
    def test_refuses_kill_when_name_is_unreadable(self):
        adapter = FakeAdapter({4242: ("", None)})
        for started_at in (None, "2026-11-05T09:12:33.123456"):
            success, message = execute_kill(adapter, 4242, started_at)
            self.assertFalse(success)
            self.assertEqual(message, UNKNOWN_PROCESS_MESSAGE)
        self.assertEqual(adapter.killed, [])

    def test_shared_lookup_maps_access_denied_to_empty_name(self):
        import sys
        import types
        from unittest import mock

        from pm_agent.platforms.base import lookup_process_name

        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        class Process:
            def __init__(self, pid):
                if pid == 1:
                    raise NoSuchProcess(pid)
                self.pid = pid

            def name(self):
                if self.pid == 2:
                    raise AccessDenied(self.pid)
                return "nginx"

        fake_psutil = types.SimpleNamespace(Process=Process, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied)
        with mock.patch.dict(sys.modules, {"psutil": fake_psutil}):
            with self.assertRaises(LookupError):
                lookup_process_name(1)
            self.assertEqual(lookup_process_name(2), "")
            self.assertEqual(lookup_process_name(3), "nginx")

    def test_adapters_share_base_implementation(self):
        """OS별 adapter가 get_process_name을 따로 구현하지 않고 base 구현을 공유하는지 확인합니다."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for relative in ("pm_agent/platforms/linux/adapter.py", "pm_agent/platforms/windows/adapter.py",
                         "pm_agent/platforms/linux/processes.py", "pm_agent/platforms/windows/processes.py",
                         "system/process.py"):
            self.assertNotIn("def get_process_name", (root / relative).read_text(encoding="utf-8"), relative)


class LinuxStartedAtFormatTests(unittest.TestCase):
    """kill 검증용 started_at이 프로세스 목록과 같은 format_started_at을 쓰는지 확인합니다."""

    def test_lookup_uses_same_formatter_as_process_list(self):
        import importlib
        import sys
        import types
        from unittest import mock

        create_time = 1793870000.654321

        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        class Process:
            def __init__(self, pid):
                if pid != 4242:
                    raise NoSuchProcess(pid)

            def create_time(self):
                return create_time

        fake_psutil = types.SimpleNamespace(
            Process=Process, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied, cpu_count=lambda: 4,
        )

        class Router:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, *args, **kwargs):
                return lambda func: func

        fake_fastapi = types.SimpleNamespace(APIRouter=Router, HTTPException=Exception)
        with mock.patch.dict(sys.modules, {"psutil": fake_psutil, "fastapi": fake_fastapi}):
            sys.modules.pop("system.process", None)
            legacy_process = importlib.import_module("system.process")
            try:
                self.assertEqual(
                    legacy_process.get_process_started_at(4242),
                    legacy_process.format_started_at(create_time),
                )
                with self.assertRaises(LookupError):
                    legacy_process.get_process_started_at(9999)
            finally:
                sys.modules.pop("system.process", None)


if __name__ == "__main__":
    unittest.main()

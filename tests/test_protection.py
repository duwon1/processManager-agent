import os
import subprocess
import unittest
from unittest import mock

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
        # pid -> OS별 보호 사유 (다른 에이전트 인스턴스, Windows critical 프로세스 등)
        self.os_protected = {}
        self.killed = []

    def get_process_name(self, pid):
        if pid not in self.processes:
            raise LookupError(pid)
        return self.processes[pid][0]

    def get_process_started_at(self, pid):
        if pid not in self.processes:
            raise LookupError(pid)
        return self.processes[pid][1]

    def protected_process_reason(self, pid):
        return self.os_protected.get(pid)

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


class OsSpecificProtectionTests(unittest.TestCase):
    def test_refuses_target_reported_protected_by_platform(self):
        # svchost처럼 이름 목록에 없는 프로세스도 OS별 확인에서 걸리면 종료하지 않습니다.
        adapter = FakeAdapter({900: ("svchost.exe", None), 901: ("svchost.exe", None)})
        adapter.os_protected[900] = "Windows critical 프로세스"

        success, message = execute_kill(adapter, 900)
        self.assertFalse(success)
        self.assertEqual(message, "Windows critical 프로세스")

        success, _ = execute_kill(adapter, 901)
        self.assertTrue(success)
        self.assertEqual(adapter.killed, [901])

    def test_linux_refuses_main_pid_of_another_agent_service(self):
        from pm_agent.platforms.linux import processes as linux_processes

        output = "MainPID=4321\n\nMainPID=0\n\nMainPID=abc\n"
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr="")
        with mock.patch.object(linux_processes.subprocess, "run", return_value=completed) as run:
            self.assertEqual(linux_processes.agent_service_main_pids(), {4321})
            self.assertTrue(linux_processes.protected_process_reason(4321))
            self.assertIsNone(linux_processes.protected_process_reason(4322))
        self.assertEqual(run.call_args.args[0], ["systemctl", "show", "-p", "MainPID", "processmanager-agent*"])

    def test_linux_does_not_block_kill_without_systemctl(self):
        from pm_agent.platforms.linux import processes as linux_processes

        with mock.patch.object(linux_processes.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIsNone(linux_processes.protected_process_reason(4321))

    @unittest.skipUnless(os.name == "nt", "Windows 전용 API")
    def test_windows_reports_own_process_as_not_critical(self):
        from pm_agent.platforms.windows import processes as windows_processes

        self.assertIs(windows_processes.is_critical_process(os.getpid()), False)
        self.assertIsNone(windows_processes.protected_process_reason(os.getpid()))
        # 없는 PID는 핸들을 열 수 없어 '확인 불가'로 보고 막지 않습니다.
        self.assertIsNone(windows_processes.is_critical_process(0x7FFFFFF0))


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

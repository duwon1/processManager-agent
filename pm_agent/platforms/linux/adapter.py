"""Linux 전용 기능을 PlatformAdapter 형태로 조립합니다."""
from __future__ import annotations

from typing import Any

from pm_agent.platforms.base import PlatformAdapter
from pm_agent.platforms.linux import filesystem, hardware, metrics, processes, services, terminal, updater, uninstaller
from pm_agent.platforms.linux.capabilities import LINUX_CAPABILITIES


class LinuxAdapter(PlatformAdapter):
    """Linux 수집/제어 기능 전체를 공통 에이전트 인터페이스에 연결합니다."""

    name = "Linux"
    capabilities = LINUX_CAPABILITIES

    def get_self_ip(self) -> str:
        return metrics.get_self_ip()

    def collect_metrics(self) -> list[dict[str, Any]]:
        return metrics.collect_metrics()

    def list_processes(self) -> list[dict[str, Any]]:
        return processes.list_processes()

    def kill_process(self, pid: int) -> str:
        return processes.kill_process(pid)

    def list_services(self) -> list[dict[str, Any]]:
        return services.list_services()

    def control_service(self, name: str, action: str) -> str:
        return services.control_service(name, action)

    def collect_hardware(self) -> dict[str, Any]:
        return hardware.collect_hardware()

    def list_files(self, path: str) -> dict[str, Any]:
        return filesystem.list_files(path)

    def open_terminal(self, session_id: str, cols: int, rows: int) -> None:
        terminal.open_session(session_id, cols, rows)

    def write_terminal(self, session_id: str, data: str) -> None:
        terminal.write(session_id, data)

    def resize_terminal(self, session_id: str, cols: int, rows: int) -> None:
        terminal.resize(session_id, cols, rows)

    def close_terminal(self, session_id: str) -> None:
        terminal.close_session(session_id)

    def iter_terminal_queues(self) -> list[tuple[str, Any]]:
        return terminal.iter_queues()

    def cleanup_terminals(self) -> None:
        terminal.cleanup_all()

    async def self_update(self, agent_dir: str) -> tuple[bool, str]:
        return await updater.self_update(agent_dir)

    def start_self_uninstall(self, agent_dir: str, service_name: str) -> None:
        uninstaller.start_self_uninstall(agent_dir, service_name)

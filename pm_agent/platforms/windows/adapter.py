"""Windows feature adapter."""
from __future__ import annotations

from typing import Any

from pm_agent.platforms.base import PlatformAdapter
from pm_agent.platforms.windows import metrics, uninstaller
from pm_agent.platforms.windows.capabilities import WINDOWS_CAPABILITIES


class WindowsAdapter(PlatformAdapter):
    """Windows monitoring and lifecycle adapter.

    Real-time metrics and web-triggered self-uninstall are supported first.
    Unsupported interactive features intentionally return empty data or explicit
    errors so the monitoring loop can keep running.
    """

    name = "Windows"
    capabilities = WINDOWS_CAPABILITIES

    def get_self_ip(self) -> str:
        return metrics.get_self_ip()

    def collect_metrics(self) -> list[dict[str, Any]]:
        return metrics.collect_metrics()

    def list_processes(self) -> list[dict[str, Any]]:
        return []

    def kill_process(self, pid: int) -> str:
        raise RuntimeError("Windows 프로세스 제어는 아직 지원하지 않습니다.")

    def list_services(self) -> list[dict[str, Any]]:
        return []

    def control_service(self, name: str, action: str) -> str:
        raise RuntimeError("Windows 서비스 제어는 아직 지원하지 않습니다.")

    def collect_hardware(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "osType": "Windows",
            "capabilities": WINDOWS_CAPABILITIES,
            "summary": {},
            "sections": [],
            "cpu": {},
            "memory": {},
            "disks": [],
            "networks": [],
            "gpus": [],
        }

    def list_files(self, path: str) -> dict[str, Any]:
        return {
            "path": str(path or ""),
            "parent": "",
            "entries": [],
            "error": "Windows 파일 조회는 아직 지원하지 않습니다.",
        }

    def open_terminal(self, session_id: str, cols: int, rows: int) -> None:
        return None

    def write_terminal(self, session_id: str, data: str) -> None:
        return None

    def resize_terminal(self, session_id: str, cols: int, rows: int) -> None:
        return None

    def close_terminal(self, session_id: str) -> None:
        return None

    def iter_terminal_queues(self) -> list[tuple[str, Any]]:
        return []

    def cleanup_terminals(self) -> None:
        return None

    async def self_update(self, agent_dir: str) -> tuple[bool, str]:
        return False, "Windows 자동 업데이트는 아직 지원하지 않습니다."

    def start_self_uninstall(self, agent_dir: str, service_name: str) -> None:
        uninstaller.start_self_uninstall(agent_dir, service_name)

"""운영체제별 기능 구현이 따라야 하는 공통 인터페이스입니다."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PlatformAdapter(ABC):
    """에이전트 공통 통신 코드가 호출하는 OS별 기능 인터페이스입니다."""

    name: str

    @abstractmethod
    def get_self_ip(self) -> str:
        """서버에 보고할 에이전트의 로컬 IP를 반환합니다."""

    @abstractmethod
    def collect_metrics(self) -> list[dict[str, Any]]:
        """대시보드 실시간 모니터링 데이터를 수집합니다."""

    @abstractmethod
    def list_processes(self) -> list[dict[str, Any]]:
        """프로세스 목록을 수집합니다."""

    @abstractmethod
    def kill_process(self, pid: int) -> str:
        """PID 기준으로 프로세스를 종료하고 결과 메시지를 반환합니다."""

    def get_process_name(self, pid: int) -> str:
        """PID의 현재 프로세스 이름을 반환합니다. (Linux/Windows 공통 구현)

        - 프로세스가 없으면 LookupError를 발생시킵니다.
        - 권한 부족(AccessDenied) 등으로 이름을 읽지 못하면 ""를 반환합니다.
          protection.execute_kill은 ""를 '알 수 없는 프로세스'로 보고 종료를 거부합니다.
        """
        return lookup_process_name(pid)

    def get_process_started_at(self, pid: int) -> str | None:
        """프로세스 목록의 started_at과 같은 포맷으로 현재 시작 시각을 반환합니다.
        OS별 adapter가 구현하지 않으면 None을 반환해 startedAt 검증 시 종료를 거부합니다.
        """
        return None

    @abstractmethod
    def list_services(self) -> list[dict[str, Any]]:
        """서비스 목록을 수집합니다."""

    @abstractmethod
    def control_service(self, name: str, action: str) -> str:
        """서비스 제어 명령을 실행합니다."""

    @abstractmethod
    def collect_hardware(self) -> dict[str, Any]:
        """OS별 하드웨어 상세 정보를 수집합니다."""

    def collect_device_manager(self) -> dict[str, Any]:
        """장치 관리자형 장치/드라이버 인벤토리를 수집합니다."""
        return {
            "schemaVersion": 1,
            "osType": self.name,
            "supported": False,
            "message": "이 운영체제의 장치 관리자 정보 수집은 아직 지원하지 않습니다.",
            "summary": {
                "totalDevices": 0,
                "problemDevices": 0,
                "categoryCount": 0,
            },
            "devices": [],
            "categories": [],
            "cpu": [],
            "baseboard": {},
            "gpus": [],
            "networkAdapters": [],
        }

    @abstractmethod
    def list_files(self, path: str) -> dict[str, Any]:
        """지정 경로의 파일/디렉토리 목록을 반환합니다."""

    @abstractmethod
    def open_terminal(self, session_id: str, cols: int, rows: int, shell: str | None = None) -> None:
        """터미널 세션을 엽니다."""

    @abstractmethod
    def write_terminal(self, session_id: str, data: str) -> None:
        """터미널 세션에 입력을 전달합니다."""

    @abstractmethod
    def resize_terminal(self, session_id: str, cols: int, rows: int) -> None:
        """터미널 세션 크기를 변경합니다."""

    @abstractmethod
    def close_terminal(self, session_id: str) -> None:
        """터미널 세션을 닫습니다."""

    @abstractmethod
    def iter_terminal_queues(self) -> list[tuple[str, Any]]:
        """활성 터미널 세션의 출력 큐 목록을 반환합니다."""

    @abstractmethod
    def cleanup_terminals(self) -> None:
        """열려 있는 터미널 세션을 모두 정리합니다."""

    @abstractmethod
    async def self_update(self, agent_dir: str, target_sha: str = "") -> tuple[bool, str]:
        """에이전트 코드를 최신화하고 성공 여부와 메시지를 반환합니다."""

    @abstractmethod
    def start_self_uninstall(self, agent_dir: str, service_name: str) -> None:
        """자가 삭제 작업을 백그라운드로 시작합니다."""


def lookup_process_name(pid: int) -> str:
    """psutil로 PID의 프로세스 이름을 조회합니다. 모든 OS adapter가 이 한 구현을 공유합니다."""
    import psutil

    try:
        return psutil.Process(pid).name() or ""
    except psutil.NoSuchProcess as exc:
        raise LookupError(f"프로세스 {pid}를 찾을 수 없습니다.") from exc
    except (psutil.AccessDenied, OSError):
        return ""

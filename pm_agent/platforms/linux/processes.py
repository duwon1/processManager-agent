"""Linux 프로세스 수집/제어 adapter 함수입니다."""
from __future__ import annotations

import subprocess
from typing import Any

from system import process as legacy_process


def list_processes() -> list[dict[str, Any]]:
    """Linux 프로세스 목록을 대시보드 공통 형식으로 반환합니다."""
    return legacy_process.get_process_data()


def get_process_started_at(pid: int) -> str | None:
    """프로세스 목록과 같은 포맷으로 PID의 현재 started_at을 반환합니다."""
    return legacy_process.get_process_started_at(pid)


def kill_process(pid: int) -> str:
    """PID 기준으로 Linux 프로세스를 종료합니다."""
    return legacy_process.kill_process_by_pid(pid)


def agent_service_main_pids() -> set[int]:
    """systemd에 올라온 에이전트 서비스(processmanager-agent*)들의 MainPID를 반환합니다.

    systemctl이 없거나 실패하면 빈 집합을 반환합니다. (systemd가 없는 환경에서 종료 기능 전체가 막히지 않게 함)
    """
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "processmanager-agent*"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    pids: set[int] = set()
    for line in (result.stdout or "").splitlines():
        if line.startswith("MainPID="):
            try:
                main_pid = int(line[len("MainPID="):])
            except ValueError:
                continue
            if main_pid > 0:
                pids.add(main_pid)
    return pids


def protected_process_reason(pid: int) -> str | None:
    """같은 서버의 다른 에이전트 인스턴스면 거부 사유를 반환합니다.

    에이전트와 같은 사용자로 도는 프로세스는 root 래퍼(pm-kill)를 거치지 않고 os.kill로 바로 종료되므로,
    래퍼가 하는 MainPID 보호를 여기서도 같은 기준으로 확인합니다.
    """
    if pid in agent_service_main_pids():
        return "에이전트 서비스 프로세스는 종료할 수 없습니다."
    return None

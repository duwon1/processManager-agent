"""Linux 프로세스 수집/제어 adapter 함수입니다."""
from __future__ import annotations

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

"""Linux root 권한 작업에 쓰는 래퍼 스크립트 경로와 sudo 명령 선택 helper입니다.

install.sh가 /usr/local/lib/processmanager 아래에 root 소유(0755) 래퍼를 설치하고,
sudoers는 와일드카드 대신 이 래퍼 경로만 허용합니다. 래퍼가 인자를 하나씩 검증하므로
`kill -9 1 -1`이나 `systemctl start poweroff.target x.service` 같은 인자 끼워넣기를 막습니다.
래퍼가 없는 구버전 설치에서는 기존 직접 명령으로 대체합니다.
"""
from __future__ import annotations

import os
import shutil

WRAPPER_DIR = "/usr/local/lib/processmanager"
PM_KILL_PATH = f"{WRAPPER_DIR}/pm-kill"
PM_SERVICE_PATH = f"{WRAPPER_DIR}/pm-service"


def wrapper_available(path: str) -> bool:
    """래퍼가 실행 가능한 일반 파일로 설치돼 있는지 확인합니다."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def kill_command(pid: int) -> list[str]:
    """PID 강제 종료에 쓸 sudo 명령을 반환합니다. 래퍼가 있으면 래퍼를 우선 사용합니다."""
    if wrapper_available(PM_KILL_PATH):
        return ["sudo", "-n", PM_KILL_PATH, str(pid)]
    kill_bin = shutil.which("kill") or "/usr/bin/kill"
    return ["sudo", "-n", kill_bin, "-9", str(pid)]


def service_command(action: str, unit: str) -> list[str]:
    """서비스 제어에 쓸 sudo 명령을 반환합니다. 래퍼가 있으면 래퍼를 우선 사용합니다."""
    if wrapper_available(PM_SERVICE_PATH):
        return ["sudo", "-n", PM_SERVICE_PATH, action, unit]
    systemctl_bin = shutil.which("systemctl") or "/usr/bin/systemctl"
    return ["sudo", "-n", systemctl_bin, action, unit]

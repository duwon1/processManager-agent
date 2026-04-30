"""Linux systemd 서비스 adapter 함수입니다."""
from __future__ import annotations

from typing import Any

from system import services as legacy_services


def list_services() -> list[dict[str, Any]]:
    """systemd 서비스 목록을 반환합니다."""
    return legacy_services.get_service_list()


def control_service(name: str, action: str) -> str:
    """systemctl로 Linux 서비스를 제어합니다."""
    return legacy_services.control_service(name, action)

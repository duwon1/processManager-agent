"""Linux 메트릭 수집 adapter 함수입니다."""
from __future__ import annotations

from typing import Any

from system import metrics as legacy_metrics


def get_self_ip() -> str:
    """서버에 보고할 Linux 호스트의 로컬 IP를 반환합니다."""
    return legacy_metrics.get_self_ip()


def collect_metrics() -> list[dict[str, Any]]:
    """Linux 시스템 메트릭을 표준 숫자 단위로 수집합니다."""
    return legacy_metrics.collect_system_metrics()

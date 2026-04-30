"""현재 실행 환경에 맞는 PlatformAdapter를 선택합니다."""
from __future__ import annotations

import platform

from pm_agent.platforms.base import PlatformAdapter


def get_platform_adapter(os_type: str | None = None) -> PlatformAdapter:
    """설정값 또는 실제 OS 이름으로 adapter를 생성합니다."""
    detected = (os_type or platform.system()).strip().lower()
    if detected in {"linux", "linux-server"}:
        # Linux 전용 모듈은 fcntl/pty 같은 Unix 의존성이 있으므로 실제 선택 시점에만 import합니다.
        from pm_agent.platforms.linux.adapter import LinuxAdapter

        return LinuxAdapter()
    raise RuntimeError(f"지원하지 않는 운영체제입니다: {os_type or platform.system()}")

"""Linux 에이전트 self-uninstall 기능입니다."""
from __future__ import annotations

import shlex
import subprocess


def start_self_uninstall(agent_dir: str, service_name: str) -> None:
    """systemd 서비스와 설치 디렉토리를 백그라운드에서 제거합니다."""
    safe_service_name = shlex.quote(service_name)
    safe_agent_dir = shlex.quote(agent_dir)
    cmds = " && ".join([
        f"sudo systemctl disable {safe_service_name} 2>/dev/null || true",
        f"sudo systemctl stop {safe_service_name} 2>/dev/null || true",
        f"sudo rm -f /etc/systemd/system/{safe_service_name}.service 2>/dev/null || true",
        "sudo systemctl daemon-reload 2>/dev/null || true",
        f"rm -rf {safe_agent_dir}",
    ])
    subprocess.Popen(["bash", "-c", f"sleep 2 && {cmds}"])

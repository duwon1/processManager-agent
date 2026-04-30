"""Linux 에이전트 self-update 기능입니다."""
from __future__ import annotations

import asyncio
import shlex
import subprocess


async def self_update(agent_dir: str) -> tuple[bool, str]:
    """git pull과 의존성 설치를 현재 프로세스에서 수행합니다."""
    safe_agent_dir = shlex.quote(agent_dir)
    cmds = " && ".join([
        f"git -C {safe_agent_dir} pull origin master",
        f"{safe_agent_dir}/.venv/bin/pip install -r {safe_agent_dir}/requirements.txt -q",
    ])
    result = await asyncio.to_thread(
        subprocess.run,
        ["bash", "-lc", cmds],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    output = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0:
        return False, output or "업데이트 실패"
    return True, output or "업데이트 적용 완료"

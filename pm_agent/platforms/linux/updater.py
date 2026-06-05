"""Linux 에이전트 self-update 기능입니다."""
from __future__ import annotations

import asyncio
import shlex
import subprocess

from pm_agent.update_policy import normalize_target_sha


async def self_update(agent_dir: str, target_sha: str = "") -> tuple[bool, str]:
    """git pull과 의존성 설치를 현재 프로세스에서 수행합니다."""
    requested_target_sha = str(target_sha or "").strip()
    normalized_target_sha = normalize_target_sha(requested_target_sha)
    if requested_target_sha and not normalized_target_sha:
        return False, "invalid targetSha"

    safe_agent_dir = shlex.quote(agent_dir)
    update_commands = [f"git -C {safe_agent_dir} fetch origin master"]
    if normalized_target_sha:
        update_commands.append(f"git -C {safe_agent_dir} checkout --detach {shlex.quote(normalized_target_sha)}")
    else:
        update_commands.extend([
            f"git -C {safe_agent_dir} checkout master",
            f"git -C {safe_agent_dir} pull --ff-only origin master",
        ])
    dependency_command = (
        f"if [ -f {safe_agent_dir}/requirements.lock ]; then "
        f"PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 {safe_agent_dir}/.venv/bin/python -m pip install "
        f"--no-cache-dir --disable-pip-version-check --require-hashes -r {safe_agent_dir}/requirements.lock -q; "
        f"else "
        f"PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 {safe_agent_dir}/.venv/bin/python -m pip install "
        f"--no-cache-dir --disable-pip-version-check -r {safe_agent_dir}/requirements.txt -q; "
        f"fi"
    )
    cmds = " && ".join([
        *update_commands,
        dependency_command,
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

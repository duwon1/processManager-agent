"""Windows agent self-update support."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import textwrap
import uuid
from pathlib import Path


async def self_update(agent_dir: str) -> tuple[bool, str]:
    """Pull the latest agent source, refresh dependencies, and restart the scheduled task."""
    target_dir = Path(agent_dir).resolve()
    if not _looks_like_agent_dir(target_dir):
        return False, f"unexpected agent directory: {target_dir}"

    git_path = shutil.which("git")
    if not git_path:
        return False, "Git을 찾을 수 없어 Windows 에이전트를 업데이트할 수 없습니다."

    pull_result = await asyncio.to_thread(
        subprocess.run,
        [git_path, "-C", str(target_dir), "pull", "origin", "master"],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if pull_result.returncode != 0:
        return False, _command_output(pull_result) or "git pull failed"

    python_path = target_dir / ".venv" / "Scripts" / "python.exe"
    requirements_path = target_dir / "requirements.txt"
    if not python_path.exists():
        return False, f"Python venv not found: {python_path}"

    pip_result = await asyncio.to_thread(
        subprocess.run,
        [str(python_path), "-m", "pip", "install", "-r", str(requirements_path), "-q"],
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if pip_result.returncode != 0:
        return False, _command_output(pip_result) or "pip install failed"

    task_name = os.getenv("SERVICE_NAME", "ProcessManagerAgent").strip() or "ProcessManagerAgent"
    restart_script = _write_restart_script()
    _start_detached_restart(restart_script, task_name)

    message = _command_output(pull_result) or "업데이트 적용 완료"
    return True, message


def _looks_like_agent_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if path.parent == path:
        return False
    required_files = ["main.py", "agent.py", "config.py", ".env"]
    return all((path / name).exists() for name in required_files)


def _command_output(result: subprocess.CompletedProcess[str]) -> str:
    return ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-400:]


def _start_detached_restart(script_path: Path, task_name: str) -> None:
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS

    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
            "-TaskName",
            task_name,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _write_restart_script() -> Path:
    script_path = Path(tempfile.gettempdir()) / f"processmanager-agent-restart-{uuid.uuid4().hex}.ps1"
    script = r'''
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName
)

$ErrorActionPreference = "SilentlyContinue"
Start-Sleep -Seconds 3

for ($i = 0; $i -lt 30; $i++) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task -or $task.State -ne "Running") {
        break
    }
    Start-Sleep -Seconds 1
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
'''
    script_path.write_text(textwrap.dedent(script).strip() + os.linesep, encoding="utf-8")
    return script_path

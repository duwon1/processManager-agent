"""Windows agent self-update support."""
from __future__ import annotations

import asyncio
import base64
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
        **_hidden_subprocess_kwargs(),
    )
    if pull_result.returncode != 0:
        return False, _command_output(pull_result) or "git pull failed"

    python_path = target_dir / ".venv" / "Scripts" / "python.exe"
    requirements_path = target_dir / "requirements.txt"
    if not python_path.exists():
        return False, f"Python venv not found: {python_path}"

    pip_env = os.environ.copy()
    pip_env["PIP_NO_CACHE_DIR"] = "1"
    pip_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    pip_result = await asyncio.to_thread(
        subprocess.run,
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "-r",
            str(requirements_path),
            "-q",
        ],
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
        env=pip_env,
        **_hidden_subprocess_kwargs(),
    )
    if pip_result.returncode != 0:
        return False, _command_output(pip_result) or "pip install failed"

    _write_runner_script(target_dir)

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


def _hidden_subprocess_kwargs(detached: bool = False) -> dict:
    kwargs = {}
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW
    if detached and hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS
    if creationflags:
        kwargs["creationflags"] = creationflags

    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo

    return kwargs


def _write_runner_script(agent_dir: Path) -> None:
    runner_path = agent_dir / "run-agent.ps1"
    script = r'''
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir "agent.log"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$env:PYTHONUNBUFFERED = "1"
& $python main.py >> $logFile 2>&1
exit $LASTEXITCODE
'''
    runner_path.write_text(textwrap.dedent(script).strip() + os.linesep, encoding="utf-8")


def _start_detached_restart(script_path: Path, task_name: str) -> None:
    restart_task_name = f"ProcessManagerAgent-Restart-{uuid.uuid4().hex}"
    if _start_scheduled_restart(script_path, task_name, restart_task_name):
        return
    _start_direct_restart(script_path, task_name)


def _start_scheduled_restart(script_path: Path, task_name: str, restart_task_name: str) -> bool:
    """Run restart work from a separate temporary scheduled task.

    Stop-ScheduledTask can terminate the original task process tree. If the restart
    script is only a child of that task, it can be killed before Start-ScheduledTask
    runs. A temporary task avoids that self-kill path.
    """
    register_script = f"""
$ErrorActionPreference = "Stop"
$taskName = {_ps_quote(task_name)}
$restartTaskName = {_ps_quote(restart_task_name)}
$scriptPath = {_ps_quote(str(script_path))}
$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -TaskName `"$taskName`" -CleanupTaskName `"$restartTaskName`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $restartTaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $restartTaskName
"""
    encoded = base64.b64encode(textwrap.dedent(register_script).encode("utf-16le")).decode("ascii")

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        stdin=subprocess.DEVNULL,
        **_hidden_subprocess_kwargs(),
    )
    return result.returncode == 0


def _start_direct_restart(script_path: Path, task_name: str) -> None:
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
        **_hidden_subprocess_kwargs(detached=True),
    )


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _write_restart_script() -> Path:
    script_path = Path(tempfile.gettempdir()) / f"processmanager-agent-restart-{uuid.uuid4().hex}.ps1"
    script = r'''
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName,

    [string]$CleanupTaskName = ""
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
        for ($i = 0; $i -lt 20; $i++) {
            $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            if (-not $task -or $task.State -ne "Running") {
                break
            }
            Start-Sleep -Seconds 1
        }
    }
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

if ($CleanupTaskName) {
    Unregister-ScheduledTask -TaskName $CleanupTaskName -Confirm:$false -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
'''
    script_path.write_text(textwrap.dedent(script).strip() + os.linesep, encoding="utf-8")
    return script_path

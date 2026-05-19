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


def ensure_runtime_layout(agent_dir: str, task_name: str) -> tuple[bool, str]:
    target_dir = Path(agent_dir).resolve()
    if not _looks_like_agent_dir(target_dir):
        return False, f"unexpected agent directory: {target_dir}"

    try:
        runner_path = _write_runner_scripts(target_dir)
        launcher_success, launcher_message = _update_task_registration(target_dir, task_name, runner_path)
        cleanup_success, cleanup_message = _cleanup_restart_tasks()
    except Exception as exc:
        return False, str(exc)

    messages = [launcher_message, cleanup_message]
    if not launcher_success or not cleanup_success:
        return False, "; ".join(message for message in messages if message)
    return True, "Windows runtime layout checked"


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

    task_name = os.getenv("SERVICE_NAME", "ProcessManagerAgent").strip() or "ProcessManagerAgent"
    layout_success, layout_message = ensure_runtime_layout(str(target_dir), task_name)
    if not layout_success:
        return False, layout_message

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


def _write_runner_scripts(agent_dir: Path) -> Path:
    ps1_path = agent_dir / "run-agent.ps1"
    pyw_path = agent_dir / "run-agent.pyw"
    ps1_script = r'''
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logFile = Join-Path $logDir "agent.log"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$env:PYTHONUNBUFFERED = "1"
$ErrorActionPreference = "Continue"
& $python main.py >> $logFile 2>&1
exit $LASTEXITCODE
'''
    pyw_script = r'''
from pathlib import Path
import os
import runpy
import sys
import traceback

base_dir = Path(__file__).resolve().parent
os.chdir(base_dir)

log_dir = base_dir / "logs"
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "agent.log"

with log_file.open("a", encoding="utf-8", buffering=1) as log:
    sys.stdout = log
    sys.stderr = log
    os.environ["PYTHONUNBUFFERED"] = "1"
    try:
        runpy.run_path(str(base_dir / "main.py"), run_name="__main__")
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise
'''
    ps1_path.write_text(textwrap.dedent(ps1_script).strip() + os.linesep, encoding="utf-8")
    pyw_path.write_text(textwrap.dedent(pyw_script).strip() + os.linesep, encoding="utf-8")
    return pyw_path


def _update_task_registration(agent_dir: Path, task_name: str, runner_path: Path) -> tuple[bool, str]:
    pythonw_path = agent_dir / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw_path.exists():
        return False, f"pythonw not found: {pythonw_path}"

    script = f"""
$ErrorActionPreference = "Stop"
$taskName = {_ps_quote(task_name)}
$agentDir = {_ps_quote(str(agent_dir))}
$pythonw = {_ps_quote(str(pythonw_path))}
$runner = {_ps_quote(str(runner_path))}
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {{
    Write-Output "scheduled task not found"
    exit 0
}}
$expectedArgument = "`"$runner`""
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $expectedArgument -WorkingDirectory $agentDir
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
Set-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logonTrigger, $watchdogTrigger) -Settings $settings | Out-Null
Write-Output "task registration updated"
"""
    result = _run_hidden_powershell(script, timeout=20)
    success = result.returncode == 0
    return success, _command_output(result) or ("task registration updated" if success else "task registration update failed")


def _cleanup_restart_tasks() -> tuple[bool, str]:
    script = r'''
$ErrorActionPreference = "Stop"
$tasks = @(Get-ScheduledTask -TaskName "ProcessManagerAgent-Restart-*" -ErrorAction SilentlyContinue)
foreach ($task in $tasks) {
    Stop-ScheduledTask -TaskName $task.TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
Write-Output "restart task cleanup: $($tasks.Count)"
'''
    result = _run_hidden_powershell(script, timeout=20)
    success = result.returncode == 0
    return success, _command_output(result) or ("restart tasks cleaned" if success else "restart task cleanup failed")


def _run_hidden_powershell(script: str, timeout: int) -> subprocess.CompletedProcess[str]:
    encoded = base64.b64encode(textwrap.dedent(script).encode("utf-16le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        **_hidden_subprocess_kwargs(),
    )


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
$launcherPath = {_ps_quote(str(_write_restart_launcher(script_path, task_name, restart_task_name)))}
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$launcherPath`""
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


def _vbs_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _write_restart_launcher(script_path: Path, task_name: str, restart_task_name: str) -> Path:
    launcher_path = Path(tempfile.gettempdir()) / f"processmanager-agent-restart-{uuid.uuid4().hex}.vbs"
    command = " ".join([
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        _vbs_quote(str(script_path)),
        "-TaskName",
        _vbs_quote(task_name),
        "-CleanupTaskName",
        _vbs_quote(restart_task_name),
        "-CleanupLauncherPath",
        _vbs_quote(str(launcher_path)),
    ])
    script = f'''
Set shell = CreateObject("WScript.Shell")
shell.Run {_vbs_quote(command)}, 0, False
'''
    launcher_path.write_text(textwrap.dedent(script).strip() + os.linesep, encoding="utf-8")
    return launcher_path


def _write_restart_script() -> Path:
    script_path = Path(tempfile.gettempdir()) / f"processmanager-agent-restart-{uuid.uuid4().hex}.ps1"
    script = r'''
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName,

    [string]$CleanupTaskName = "",

    [string]$CleanupLauncherPath = ""
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

if ($CleanupLauncherPath) {
    Remove-Item -LiteralPath $CleanupLauncherPath -Force -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
'''
    script_path.write_text(textwrap.dedent(script).strip() + os.linesep, encoding="utf-8")
    return script_path

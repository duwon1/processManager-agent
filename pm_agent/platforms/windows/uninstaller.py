"""Windows agent self-uninstall support."""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import uuid
from pathlib import Path


def start_self_uninstall(agent_dir: str, task_name: str) -> None:
    """Start a detached PowerShell cleanup process for the scheduled task install."""
    target_dir = Path(agent_dir).resolve()
    if not _looks_like_agent_dir(target_dir):
        raise RuntimeError(f"refusing to uninstall unexpected agent directory: {target_dir}")

    cleanup_script = _write_cleanup_script()
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
            str(cleanup_script),
            "-AgentDir",
            str(target_dir),
            "-TaskName",
            task_name,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _looks_like_agent_dir(path: Path) -> bool:
    """Avoid launching a recursive delete for an obviously wrong path."""
    if not path.is_dir():
        return False
    if path.parent == path:
        return False
    required_files = ["main.py", "agent.py", "config.py", ".env"]
    return all((path / name).exists() for name in required_files)


def _write_cleanup_script() -> Path:
    script_path = Path(tempfile.gettempdir()) / f"processmanager-agent-uninstall-{uuid.uuid4().hex}.ps1"
    script = r'''
param(
    [Parameter(Mandatory = $true)]
    [string]$AgentDir,

    [Parameter(Mandatory = $true)]
    [string]$TaskName
)

$ErrorActionPreference = "SilentlyContinue"
Start-Sleep -Seconds 5

function Stop-AgentProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetDir
    )

    $escaped = [System.Management.Automation.WildcardPattern]::Escape($TargetDir)
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -like "*$escaped*"
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

& schtasks.exe /End /TN $TaskName 2>$null | Out-Null
& schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null

for ($i = 0; $i -lt 30; $i++) {
    Stop-AgentProcesses -TargetDir $AgentDir
    Remove-Item -LiteralPath $AgentDir -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $AgentDir)) {
        break
    }
    Start-Sleep -Seconds 1
}

Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
'''
    script_path.write_text(textwrap.dedent(script).strip() + os.linesep, encoding="utf-8")
    return script_path

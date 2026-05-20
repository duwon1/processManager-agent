"""Windows service listing and control helpers."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any


POWERSHELL = "powershell.exe"


def list_services() -> list[dict[str, Any]]:
    """Return Windows services in the same shape used by the web UI."""
    command = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"
Get-CimInstance -ClassName Win32_Service |
    Select-Object Name, DisplayName, State, Status, StartMode, Description |
    ConvertTo-Json -Compress -Depth 3
"""
    result = _run_powershell(command, timeout=20)
    if result.returncode != 0:
        print(f"[Windows 서비스] 목록 수집 오류: {_error_detail(result)}")
        return []

    raw = result.stdout.strip()
    if not raw:
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[Windows 서비스] JSON 파싱 오류: {exc}")
        return []

    rows = payload if isinstance(payload, list) else [payload]
    services: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("Name"))
        if not name:
            continue

        display_name = _text(row.get("DisplayName")) or name
        description = _text(row.get("Description"))
        state = _text(row.get("State"))
        status = _text(row.get("Status"))
        active_state, sub_state = _map_service_state(state, status)

        services.append({
            "name": name,
            "loadState": _text(row.get("StartMode")) or "unknown",
            "activeState": active_state,
            "subState": sub_state,
            "description": display_name if display_name != name else description,
            "displayName": display_name,
            "status": status,
        })

    services.sort(key=lambda item: item["name"].lower())
    return services


def control_service(name: str, action: str) -> str:
    """Start, stop, or restart a Windows service."""
    service_name = (name or "").strip()
    service_action = (action or "").strip().lower()
    allowed = {"start", "stop", "restart"}
    if not service_name:
        raise ValueError("서비스명이 없습니다.")
    if service_action not in allowed:
        raise ValueError(f"허용되지 않은 액션: {action}")

    command = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"
$name = $env:PM_SERVICE_NAME
$action = $env:PM_SERVICE_ACTION
if ([string]::IsNullOrWhiteSpace($name)) {
    throw "서비스명이 없습니다."
}
switch ($action) {
    "start" {
        Start-Service -Name $name -ErrorAction Stop
    }
    "stop" {
        Stop-Service -Name $name -Force -ErrorAction Stop
    }
    "restart" {
        Restart-Service -Name $name -Force -ErrorAction Stop
    }
    default {
        throw "허용되지 않은 액션: $action"
    }
}
$updated = Get-Service -Name $name -ErrorAction Stop
[PSCustomObject]@{
    Name = $updated.Name
    Status = $updated.Status.ToString()
} | ConvertTo-Json -Compress -Depth 2
"""
    env = os.environ.copy()
    env["PM_SERVICE_NAME"] = service_name
    env["PM_SERVICE_ACTION"] = service_action
    result = _run_powershell(command, timeout=40, env=env)
    if result.returncode != 0:
        raise RuntimeError(_error_detail(result))

    status = ""
    try:
        payload = json.loads(result.stdout.strip() or "{}")
        if isinstance(payload, dict):
            status = _text(payload.get("Status"))
    except json.JSONDecodeError:
        status = ""

    action_label = {
        "start": "시작",
        "stop": "중지",
        "restart": "재시작",
    }[service_action]
    suffix = f" ({status})" if status else ""
    return f"{service_name} {action_label} 완료{suffix}"


def _run_powershell(command: str, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


def _map_service_state(state: str, status: str) -> tuple[str, str]:
    normalized = state.strip().lower()
    if normalized == "running":
        return "active", "running"
    if normalized in {"start pending", "continue pending"}:
        return "activating", normalized.replace(" ", "-")
    if normalized in {"stop pending", "pause pending"}:
        return "deactivating", normalized.replace(" ", "-")
    if normalized == "paused":
        return "inactive", "paused"
    if normalized == "stopped":
        return "inactive", "dead"

    status_normalized = status.strip().lower()
    if status_normalized and status_normalized not in {"ok", "service"}:
        return "failed", status_normalized
    return "inactive", normalized or "unknown"


def _error_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "Windows 서비스 명령 실행에 실패했습니다.").strip()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()

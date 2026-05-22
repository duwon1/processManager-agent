"""Windows Device Manager-style inventory collection."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 1


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW
    if creationflags:
        kwargs["creationflags"] = creationflags
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _run_powershell_json(script: str, timeout: float = 60) -> Any:
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"{script.strip()} | ConvertTo-Json -Compress -Depth 8"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_hidden_subprocess_kwargs(),
        )
    except Exception as exc:
        return {"error": str(exc)}

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {"error": detail or f"PowerShell exited with code {result.returncode}"}
    if not result.stdout.strip():
        return {}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"PowerShell JSON parse failed: {exc}"}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_rows(value: Any) -> list[dict[str, Any]]:
    rows = []
    for row in _as_list(value):
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _category_label(value: str | None) -> str:
    labels = {
        "processor": "프로세서",
        "display": "디스플레이 어댑터",
        "net": "네트워크 어댑터",
        "diskdrive": "디스크 드라이브",
        "hdc": "저장소 컨트롤러",
        "usb": "USB 컨트롤러",
        "system": "시스템 장치",
        "media": "사운드/비디오/게임 컨트롤러",
        "monitor": "모니터",
        "keyboard": "키보드",
        "mouse": "마우스 및 포인팅 장치",
        "bluetooth": "Bluetooth",
        "battery": "배터리",
        "camera": "카메라",
        "computer": "컴퓨터",
        "softwaredevice": "소프트웨어 장치",
        "ports": "포트",
        "printqueue": "인쇄 큐",
        "printer": "프린터",
        "securitydevices": "보안 장치",
        "firmware": "펌웨어",
    }
    normalized = (value or "unknown").strip().lower()
    return labels.get(normalized, value or "기타 장치")


def _normalize_device(row: dict[str, Any]) -> dict[str, Any]:
    problem_code = _to_int(row.get("ProblemCode"))
    return {
        "name": _clean_text(row.get("Name")) or _clean_text(row.get("DeviceName")),
        "category": _clean_text(row.get("PNPClass")) or "Unknown",
        "categoryLabel": _category_label(_clean_text(row.get("PNPClass"))),
        "manufacturer": _clean_text(row.get("Manufacturer")),
        "status": _clean_text(row.get("Status")),
        "service": _clean_text(row.get("Service")),
        "present": row.get("Present"),
        "problemCode": problem_code,
        "hasProblem": problem_code not in (None, 0),
        "deviceId": _clean_text(row.get("DeviceID")),
        "pnpDeviceId": _clean_text(row.get("PNPDeviceID")) or _clean_text(row.get("DeviceID")),
        "classGuid": _clean_text(row.get("ClassGuid")),
        "driverProvider": _clean_text(row.get("DriverProvider")),
        "driverVersion": _clean_text(row.get("DriverVersion")),
        "driverDate": _clean_text(row.get("DriverDate")),
        "driverInf": _clean_text(row.get("DriverInf")),
        "driverSigner": _clean_text(row.get("DriverSigner")),
    }


def _device_key(device: dict[str, Any]) -> str:
    return str(device.get("pnpDeviceId") or device.get("deviceId") or device.get("name") or "").lower()


def _lookup_device(devices: list[dict[str, Any]], *candidates: Any) -> dict[str, Any]:
    normalized = {_device_key(device): device for device in devices if _device_key(device)}
    for candidate in candidates:
        key = _clean_text(candidate)
        if not key:
            continue
        match = normalized.get(key.lower())
        if match:
            return match
    return {}


def _normalize_cpu(row: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any]:
    device = _lookup_device(devices, row.get("PNPDeviceID"), row.get("DeviceID"))
    return {
        "name": _clean_text(row.get("Name")),
        "manufacturer": _clean_text(row.get("Manufacturer")),
        "deviceId": _clean_text(row.get("DeviceID")),
        "pnpDeviceId": _clean_text(row.get("PNPDeviceID")),
        "socket": _clean_text(row.get("SocketDesignation")),
        "cores": _to_int(row.get("NumberOfCores")),
        "logicalProcessors": _to_int(row.get("NumberOfLogicalProcessors")),
        "maxClockMhz": _to_int(row.get("MaxClockSpeed")),
        "currentClockMhz": _to_int(row.get("CurrentClockSpeed")),
        "status": _clean_text(row.get("Status")) or device.get("status"),
        "driverProvider": device.get("driverProvider"),
        "driverVersion": device.get("driverVersion"),
        "driverDate": device.get("driverDate"),
    }


def _normalize_gpu(row: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any]:
    device = _lookup_device(devices, row.get("PNPDeviceID"), row.get("DeviceID"))
    return {
        "name": _clean_text(row.get("Name")),
        "manufacturer": _clean_text(row.get("AdapterCompatibility")) or device.get("manufacturer"),
        "videoProcessor": _clean_text(row.get("VideoProcessor")),
        "adapterRamBytes": _to_int(row.get("AdapterRAM")),
        "videoMode": _clean_text(row.get("VideoModeDescription")),
        "pnpDeviceId": _clean_text(row.get("PNPDeviceID")),
        "status": _clean_text(row.get("Status")) or device.get("status"),
        "problemCode": _to_int(row.get("ConfigManagerErrorCode")) or device.get("problemCode"),
        "driverProvider": device.get("driverProvider"),
        "driverVersion": _clean_text(row.get("DriverVersion")) or device.get("driverVersion"),
        "driverDate": _clean_text(row.get("DriverDate")) or device.get("driverDate"),
        "driverInf": device.get("driverInf"),
    }


def _normalize_network(row: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any]:
    device = _lookup_device(devices, row.get("PNPDeviceID"), row.get("GUID"), row.get("DeviceID"))
    return {
        "name": _clean_text(row.get("Name")),
        "connectionName": _clean_text(row.get("NetConnectionID")),
        "description": _clean_text(row.get("Description")),
        "manufacturer": _clean_text(row.get("Manufacturer")) or device.get("manufacturer"),
        "adapterType": _clean_text(row.get("AdapterType")),
        "macAddress": _clean_text(row.get("MACAddress")),
        "speedBitsPerSecond": _to_int(row.get("Speed")),
        "physicalAdapter": row.get("PhysicalAdapter"),
        "netEnabled": row.get("NetEnabled"),
        "serviceName": _clean_text(row.get("ServiceName")) or device.get("service"),
        "pnpDeviceId": _clean_text(row.get("PNPDeviceID")),
        "status": _clean_text(row.get("Status")) or device.get("status"),
        "problemCode": _to_int(row.get("ConfigManagerErrorCode")) or device.get("problemCode"),
        "driverProvider": device.get("driverProvider"),
        "driverVersion": device.get("driverVersion"),
        "driverDate": device.get("driverDate"),
        "driverInf": device.get("driverInf"),
    }


def _build_categories(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        key = str(device.get("category") or "Unknown")
        grouped.setdefault(key, []).append(device)

    categories = []
    for key, rows in grouped.items():
        rows.sort(key=lambda item: str(item.get("name") or "").lower())
        categories.append({
            "key": key,
            "label": _category_label(key),
            "count": len(rows),
            "problemCount": sum(1 for item in rows if item.get("hasProblem")),
            "devices": rows,
        })
    categories.sort(key=lambda item: item["label"].lower())
    return categories


def collect_device_manager() -> dict[str, Any]:
    data = _run_powershell_json(r"""
$drivers = @{}
Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue | ForEach-Object {
  if ($_.DeviceID) { $drivers[$_.DeviceID] = $_ }
}

function Format-DateValue($value) {
  if ($null -eq $value) { return $null }
  try { return ([datetime]$value).ToString("yyyy-MM-dd") } catch { return [string]$value }
}

function Driver-For($deviceId) {
  if ($deviceId -and $drivers.ContainsKey($deviceId)) { return $drivers[$deviceId] }
  return $null
}

$devices = @(Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | ForEach-Object {
  $driver = Driver-For $_.DeviceID
  [pscustomobject]@{
    Name = $_.Name
    PNPClass = $_.PNPClass
    Manufacturer = $_.Manufacturer
    Status = $_.Status
    Service = $_.Service
    Present = $_.Present
    ProblemCode = $_.ConfigManagerErrorCode
    DeviceID = $_.DeviceID
    PNPDeviceID = $_.PNPDeviceID
    ClassGuid = $_.ClassGuid
    DriverProvider = if ($driver) { $driver.DriverProviderName } else { $null }
    DriverVersion = if ($driver) { $driver.DriverVersion } else { $null }
    DriverDate = if ($driver) { Format-DateValue $driver.DriverDate } else { $null }
    DriverInf = if ($driver) { $driver.InfName } else { $null }
    DriverSigner = if ($driver) { $driver.Signer } else { $null }
  }
})

[pscustomobject]@{
  processors = @(Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object DeviceID,Name,Manufacturer,SocketDesignation,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentClockSpeed,Status,PNPDeviceID)
  baseboards = @(Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue | Select-Object Manufacturer,Product,Version,SerialNumber,Status)
  bios = @(Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue | Select-Object Manufacturer,Name,SMBIOSBIOSVersion,Version,SerialNumber,ReleaseDate)
  computerSystem = @(Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue | Select-Object Manufacturer,Model,SystemType,PCSystemType,TotalPhysicalMemory)
  gpus = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Select-Object Name,AdapterCompatibility,VideoProcessor,AdapterRAM,VideoModeDescription,PNPDeviceID,Status,ConfigManagerErrorCode,DriverVersion,DriverDate)
  networkAdapters = @(Get-CimInstance Win32_NetworkAdapter -ErrorAction SilentlyContinue | Select-Object Name,NetConnectionID,Description,Manufacturer,AdapterType,MACAddress,Speed,PhysicalAdapter,NetEnabled,ServiceName,PNPDeviceID,GUID,Status,ConfigManagerErrorCode)
  devices = $devices
}
""")

    if not isinstance(data, dict):
        data = {}

    raw_devices = _normalize_rows(data.get("devices"))
    devices = [_normalize_device(row) for row in raw_devices]
    devices = [device for device in devices if device.get("name")]

    cpu = [
        _normalize_cpu(row, devices)
        for row in _normalize_rows(data.get("processors"))
        if _clean_text(row.get("Name"))
    ]
    gpus = [
        _normalize_gpu(row, devices)
        for row in _normalize_rows(data.get("gpus"))
        if _clean_text(row.get("Name"))
    ]
    network_adapters = [
        _normalize_network(row, devices)
        for row in _normalize_rows(data.get("networkAdapters"))
        if _clean_text(row.get("Name"))
    ]

    baseboards = _normalize_rows(data.get("baseboards"))
    bios_rows = _normalize_rows(data.get("bios"))
    computer_rows = _normalize_rows(data.get("computerSystem"))
    baseboard = baseboards[0] if baseboards else {}
    bios = bios_rows[0] if bios_rows else {}
    computer = computer_rows[0] if computer_rows else {}

    problem_devices = [device for device in devices if device.get("hasProblem")]
    categories = _build_categories(devices)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "supported": True,
        "osType": "Windows",
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "totalDevices": len(devices),
            "problemDevices": len(problem_devices),
            "categoryCount": len(categories),
            "gpuCount": len(gpus),
            "networkAdapterCount": len(network_adapters),
        },
        "cpu": cpu,
        "baseboard": {
            "manufacturer": _clean_text(baseboard.get("Manufacturer")),
            "product": _clean_text(baseboard.get("Product")),
            "version": _clean_text(baseboard.get("Version")),
            "serialNumber": _clean_text(baseboard.get("SerialNumber")),
            "status": _clean_text(baseboard.get("Status")),
            "computerManufacturer": _clean_text(computer.get("Manufacturer")),
            "computerModel": _clean_text(computer.get("Model")),
            "systemType": _clean_text(computer.get("SystemType")),
            "biosManufacturer": _clean_text(bios.get("Manufacturer")),
            "biosVersion": _clean_text(bios.get("SMBIOSBIOSVersion")) or _clean_text(bios.get("Version")),
            "biosName": _clean_text(bios.get("Name")),
            "biosSerialNumber": _clean_text(bios.get("SerialNumber")),
        },
        "gpus": gpus,
        "networkAdapters": network_adapters,
        "categories": categories,
        "devices": devices,
        "error": _clean_text(data.get("error")),
    }

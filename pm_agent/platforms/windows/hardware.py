"""Windows hardware detail collection."""
from __future__ import annotations

import copy
import json
import os
import platform
import socket
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import psutil

from pm_agent.platforms.windows.capabilities import WINDOWS_CAPABILITIES

try:
    from pm_agent.platforms.windows import native_cpu_perf
except Exception:
    native_cpu_perf = None

try:
    from pm_agent.platforms.windows import native_gpu_usage
except Exception:
    native_gpu_usage = None

try:
    from pm_agent.platforms.windows import native_gpu_inventory
except Exception:
    native_gpu_inventory = None

try:
    from pm_agent.platforms.windows import native_memory_perf
except Exception:
    native_memory_perf = None


SCHEMA_VERSION = 1
HARDWARE_SAMPLER_INTERVAL_SECONDS = 1.0
HARDWARE_SNAPSHOT_STALE_SECONDS = 10.0
STATIC_HARDWARE_CACHE_SECONDS = 600
GPU_COUNTER_CACHE_SECONDS = 30
GPU_TEMPERATURE_CACHE_SECONDS = 5
MEMORY_PERF_CACHE_SECONDS = 30

_latest_hardware_snapshot: dict[str, Any] | None = None
_latest_hardware_snapshot_time = 0.0
_latest_hardware_lock = threading.Lock()
_first_snapshot_event = threading.Event()
_sampler_started = False
_sampler_lock = threading.Lock()
_static_cache: dict[str, tuple[float, Any]] = {}
_static_cache_lock = threading.Lock()
_memory_perf_cache: dict[str, int] | None = None
_memory_perf_cache_time = 0.0
_memory_perf_refreshing = False
_memory_perf_lock = threading.Lock()
_gpu_counter_cache: dict[str, Any] | None = None
_gpu_counter_cache_time = 0.0
_gpu_counter_refreshing = False
_gpu_counter_lock = threading.Lock()
_gpu_temperature_cache: list[dict[str, Any]] | None = None
_gpu_temperature_cache_time = 0.0
_gpu_temperature_refreshing = False
_gpu_temperature_lock = threading.Lock()


def _item(key: str, value: Any, unit: str = "text") -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "valueType": "number" if isinstance(value, (int, float)) else "text",
    }


def _section(key: str, items: list[dict[str, Any]], groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"key": key, "items": items}
    if groups:
        payload["groups"] = groups
    return payload


def _cached_static(key: str, loader) -> Any:
    now = time.time()
    with _static_cache_lock:
        cached = _static_cache.get(key)
        if cached and now - cached[0] < STATIC_HARDWARE_CACHE_SECONDS:
            return copy.deepcopy(cached[1])

    value = loader()
    with _static_cache_lock:
        _static_cache[key] = (time.time(), copy.deepcopy(value))
    return value


def _get_cached_static(key: str) -> Any:
    now = time.time()
    with _static_cache_lock:
        cached = _static_cache.get(key)
        if cached and now - cached[0] < STATIC_HARDWARE_CACHE_SECONDS:
            return copy.deepcopy(cached[1])
    return None


def _run_powershell_json(script: str, timeout: float = 5) -> Any:
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW

    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"{script.strip()} | ConvertTo-Json -Compress -Depth 5"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
    except Exception:
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first_number(*values: Any) -> int | float | None:
    for value in values:
        try:
            if value is not None and str(value).strip() != "":
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _to_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_available(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _kb_to_bytes(value: Any) -> int | None:
    parsed = _to_int(value)
    return parsed * 1024 if parsed is not None else None


def _available_flag(value: Any) -> str | None:
    if isinstance(value, bool):
        return "available" if value else "unavailable"
    if value is None:
        return None
    return "available" if str(value).strip().lower() in ("true", "1", "yes") else "unavailable"


def _memory_form_factor(code: Any) -> str | None:
    names = {
        8: "DIMM",
        12: "SODIMM",
        13: "SRIMM",
        14: "SMD",
        15: "SSMP",
    }
    try:
        return names.get(int(code))
    except (TypeError, ValueError):
        return None


def _collect_physical_memory() -> dict[str, Any]:
    rows = _as_list(_cached_static(
        "physical_memory",
        lambda: _run_powershell_json(
            "Get-CimInstance Win32_PhysicalMemory | "
            "Select-Object Capacity,Speed,ConfiguredClockSpeed,FormFactor,MemoryType,SMBIOSMemoryType"
        ),
    ))
    if not rows:
        return {}

    speeds = [
        _first_number(row.get("ConfiguredClockSpeed"), row.get("Speed"))
        for row in rows
        if isinstance(row, dict)
    ]
    speeds = [speed for speed in speeds if speed]

    form_factors = [
        _memory_form_factor(row.get("FormFactor"))
        for row in rows
        if isinstance(row, dict)
    ]
    form_factors = [form_factor for form_factor in form_factors if form_factor]

    array_rows = _as_list(_cached_static(
        "physical_memory_array",
        lambda: _run_powershell_json(
            "Get-CimInstance Win32_PhysicalMemoryArray | Select-Object MemoryDevices"
        ),
    ))
    slots_total = None
    if array_rows:
        slots_total = sum(
            value for value in (_to_int(row.get("MemoryDevices")) for row in array_rows if isinstance(row, dict))
            if value is not None
        ) or None

    total_installed = sum(
        value for value in (_to_int(row.get("Capacity")) for row in rows if isinstance(row, dict))
        if value is not None
    ) or None

    return {
        "speedMtPerSecond": speeds[0] if speeds else None,
        "slotsUsed": len(rows),
        "slotsTotal": slots_total,
        "formFactor": form_factors[0] if form_factors else None,
        "installedBytes": total_installed,
    }


def _collect_cpu() -> dict[str, Any]:
    freq = psutil.cpu_freq()
    perf = native_cpu_perf.read_cpu_perf() if native_cpu_perf is not None else {}
    rows = _as_list(_cached_static(
        "cpu_inventory",
        lambda: _run_powershell_json(
            "Get-CimInstance Win32_Processor | "
            "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,"
            "CurrentClockSpeed,SocketDesignation,VirtualizationFirmwareEnabled,L2CacheSize,L3CacheSize",
            timeout=5,
        ),
    ))
    cpus = [row for row in rows if isinstance(row, dict)]
    first = cpus[0] if cpus else {}

    processor = (
        _clean_text(first.get("Name"))
        or platform.processor()
        or os.getenv("PROCESSOR_IDENTIFIER")
        or None
    )
    socket_names = {
        _clean_text(row.get("SocketDesignation"))
        for row in cpus
        if _clean_text(row.get("SocketDesignation"))
    }

    return {
        "model": processor,
        "sockets": len(socket_names) if socket_names else max(len(cpus), 1),
        "cores": _to_int(first.get("NumberOfCores")) or psutil.cpu_count(logical=False) or 1,
        "logicalProcessors": _to_int(first.get("NumberOfLogicalProcessors")) or psutil.cpu_count(logical=True) or 1,
        "baseSpeedMhz": _to_int(first.get("MaxClockSpeed")) or (round(freq.max, 1) if freq and freq.max else None),
        "currentSpeedMhz": _first_available(
            perf.get("currentSpeedMhz"),
            _to_int(first.get("CurrentClockSpeed")),
            round(freq.current, 1) if freq else None,
        ),
        "virtualization": _available_flag(first.get("VirtualizationFirmwareEnabled")),
        "l2CacheBytes": _kb_to_bytes(first.get("L2CacheSize")),
        "l3CacheBytes": _kb_to_bytes(first.get("L3CacheSize")),
        "uptimeSeconds": int(time.time() - psutil.boot_time()),
    }


def _read_memory_perf() -> dict[str, int]:
    if native_memory_perf is not None:
        perf = native_memory_perf.read_memory_perf()
        if perf:
            return perf

    data = _run_powershell_json(
        "Get-CimInstance Win32_PerfRawData_PerfOS_Memory | "
        "Select-Object CacheBytes,CommittedBytes,CommitLimit,PoolPagedBytes,PoolNonpagedBytes",
        timeout=5,
    )
    if not isinstance(data, dict):
        return {}

    result: dict[str, int] = {}
    for key in ("CacheBytes", "CommittedBytes", "CommitLimit", "PoolPagedBytes", "PoolNonpagedBytes"):
        value = _to_int(data.get(key))
        if value is not None:
            result[key] = value
    return result


def _refresh_memory_perf() -> None:
    global _memory_perf_cache, _memory_perf_cache_time, _memory_perf_refreshing

    try:
        perf = _read_memory_perf()
        with _memory_perf_lock:
            _memory_perf_cache = perf
            _memory_perf_cache_time = time.time()
    finally:
        with _memory_perf_lock:
            _memory_perf_refreshing = False


def _schedule_memory_perf_refresh() -> None:
    global _memory_perf_refreshing

    with _memory_perf_lock:
        if _memory_perf_refreshing:
            return
        _memory_perf_refreshing = True

    thread = threading.Thread(target=_refresh_memory_perf, name="pm-memory-perf", daemon=True)
    thread.start()


def _collect_memory_perf() -> dict[str, int]:
    now = time.time()
    with _memory_perf_lock:
        cached = copy.deepcopy(_memory_perf_cache)
        cache_time = _memory_perf_cache_time

    if cached is None or now - cache_time >= MEMORY_PERF_CACHE_SECONDS:
        _schedule_memory_perf_refresh()

    return cached if cached is not None else {}


def _collect_memory() -> dict[str, Any]:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    perf = _collect_memory_perf()
    physical = _collect_physical_memory()
    installed_bytes = physical.get("installedBytes")
    hardware_reserved = max(0, installed_bytes - mem.total) if installed_bytes and installed_bytes > mem.total else None
    return {
        "totalBytes": mem.total,
        "usedBytes": mem.used,
        "availableBytes": mem.available,
        "usagePercent": round(mem.percent, 1),
        "cachedBytes": perf.get("CacheBytes", getattr(mem, "cached", 0) or 0),
        "committedBytes": perf.get("CommittedBytes", mem.used + getattr(swap, "used", 0)),
        "commitLimitBytes": perf.get("CommitLimit", mem.total + getattr(swap, "total", 0)),
        "pagedPoolBytes": perf.get("PoolPagedBytes"),
        "nonPagedPoolBytes": perf.get("PoolNonpagedBytes"),
        "hardwareReservedBytes": hardware_reserved,
        **physical,
    }


def _normalize_disk_type(value: Any, fallback: Any = None) -> str | None:
    raw = _clean_text(value) or _clean_text(fallback)
    if not raw:
        return None
    upper = raw.upper()
    if "SSD" in upper:
        return "SSD"
    if "HDD" in upper or "MAGNETIC" in upper:
        return "HDD"
    if "NVME" in upper:
        return "SSD"
    return raw


def _unique_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _collect_disk_inventory() -> list[dict[str, Any]]:
    script = r"""
$physical = Get-PhysicalDisk -ErrorAction SilentlyContinue |
  Select-Object FriendlyName,MediaType,BusType,Size
Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
  $logical = $_
  $partition = Get-CimAssociatedInstance -InputObject $logical -Association Win32_LogicalDiskToPartition |
    Select-Object -First 1
  $disk = $null
  if ($partition) {
    $disk = Get-CimAssociatedInstance -InputObject $partition -Association Win32_DiskDriveToDiskPartition |
      Select-Object -First 1
  }
  $pd = $null
  if ($disk) {
    $pd = $physical |
      Where-Object { $disk.Model -like ('*' + $_.FriendlyName + '*') -or $_.FriendlyName -like ('*' + $disk.Model + '*') } |
      Select-Object -First 1
  }
  [pscustomobject]@{
    DeviceId = $logical.DeviceID
    FileSystem = $logical.FileSystem
    Size = [int64]$logical.Size
    FreeSpace = [int64]$logical.FreeSpace
    VolumeName = $logical.VolumeName
    DiskModel = $disk.Model
    DiskIndex = $disk.Index
    DiskDeviceId = $disk.DeviceID
    MediaType = $disk.MediaType
    PhysicalMediaType = $pd.MediaType
    BusType = $pd.BusType
  }
}
"""
    rows = _cached_static("disk_inventory", lambda: _run_powershell_json(script, timeout=10))
    return [_with_live_disk_usage(row) for row in _as_list(rows) if isinstance(row, dict)]


def _with_live_disk_usage(row: dict[str, Any]) -> dict[str, Any]:
    device_id = _clean_text(row.get("DeviceId"))
    if not device_id:
        return row

    try:
        usage = psutil.disk_usage(f"{device_id}\\")
    except (PermissionError, OSError):
        return row

    next_row = dict(row)
    next_row["Size"] = usage.total
    next_row["FreeSpace"] = usage.free
    return next_row


def _physical_disk_key(row: dict[str, Any]) -> str:
    disk_index = _to_int(row.get("DiskIndex"))
    if disk_index is not None:
        return f"physicaldrive{disk_index}"

    device_id = _clean_text(row.get("DiskDeviceId"))
    if device_id:
        return device_id.replace("\\\\.\\", "").lower()

    logical = _clean_text(row.get("DeviceId"))
    return (logical or "disk").lower()


def _sample_disk_io(interval: float = 0.25) -> dict[str, tuple[int, int]]:
    first = psutil.disk_io_counters(perdisk=True) or {}
    time.sleep(interval)
    second = psutil.disk_io_counters(perdisk=True) or {}

    result: dict[str, tuple[int, int]] = {}
    if interval <= 0:
        return result

    for name, before in first.items():
        after = second.get(name)
        if not after:
            continue
        read_bps = int(max(0, (after.read_bytes - before.read_bytes) / interval))
        write_bps = int(max(0, (after.write_bytes - before.write_bytes) / interval))
        result[name.lower()] = (read_bps, write_bps)
    return result


def _disk_speed_for(row: dict[str, Any], io_speeds: dict[str, tuple[int, int]]) -> tuple[int | None, int | None]:
    candidates = []
    disk_index = _to_int(row.get("DiskIndex"))
    if disk_index is not None:
        candidates.append(f"physicaldrive{disk_index}")
    device_id = _clean_text(row.get("DiskDeviceId"))
    if device_id:
        candidates.append(device_id.replace("\\\\.\\", "").lower())
    logical = _clean_text(row.get("DeviceId"))
    if logical:
        candidates.append(logical.lower())

    for candidate in candidates:
        if candidate in io_speeds:
            return io_speeds[candidate]
    return None, None


def _collect_disks() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    inventory = _collect_disk_inventory()
    io_speeds = _sample_disk_io()

    if inventory:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in inventory:
            grouped.setdefault(_physical_disk_key(row), []).append(row)

        for rows in grouped.values():
            total = sum(value for value in (_to_int(row.get("Size")) for row in rows) if value is not None)
            free = sum(value for value in (_to_int(row.get("FreeSpace")) for row in rows) if value is not None)
            used = total - free if total else None
            percent = round((used / total) * 100, 1) if total and used is not None else None

            mountpoints = []
            for row in rows:
                device_id = _clean_text(row.get("DeviceId"))
                if device_id:
                    mountpoints.append(f"{device_id}\\")
            partitions = ", ".join(_unique_text(mountpoints))

            representative = rows[0]
            read_bps, write_bps = _disk_speed_for(representative, io_speeds)
            disk_index = _to_int(representative.get("DiskIndex"))
            physical_device = (
                _clean_text(representative.get("DiskDeviceId"))
                or (f"PhysicalDrive{disk_index}" if disk_index is not None else None)
            )

            disks.append({
                "mountpoint": partitions,
                "partitions": partitions,
                "device": physical_device or ", ".join(_unique_text([row.get("DeviceId") for row in rows])),
                "fstype": ", ".join(_unique_text([row.get("FileSystem") for row in rows])),
                "totalBytes": total or None,
                "usedBytes": used,
                "freeBytes": free if total else None,
                "usagePercent": percent,
                "capacityUsagePercent": percent,
                "readBytesPerSecond": read_bps,
                "writeBytesPerSecond": write_bps,
                "type": _normalize_disk_type(representative.get("PhysicalMediaType"), representative.get("BusType")),
                "model": _clean_text(representative.get("DiskModel")),
            })
        return disks

    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue

        read_bps, write_bps = _disk_speed_for({"DeviceId": partition.device.rstrip("\\")}, io_speeds)
        disks.append({
            "mountpoint": partition.mountpoint,
            "partitions": partition.mountpoint,
            "device": partition.device,
            "fstype": partition.fstype,
            "totalBytes": usage.total,
            "usedBytes": usage.used,
            "freeBytes": usage.free,
            "usagePercent": round(usage.percent, 1),
            "capacityUsagePercent": round(usage.percent, 1),
            "readBytesPerSecond": read_bps,
            "writeBytesPerSecond": write_bps,
            "type": None,
            "model": None,
        })
    return disks


def _is_loopback(name: str) -> bool:
    normalized = name.strip().lower()
    return "loopback" in normalized or normalized.startswith("lo") or "localhost" in normalized


def _connection_type(name: str) -> str:
    normalized = name.lower()
    if "wi-fi" in normalized or "wifi" in normalized or "wireless" in normalized or "wlan" in normalized:
        return "wifi"
    return "ethernet"


def _collect_network_inventory() -> dict[str, dict[str, Any]]:
    rows = _as_list(_cached_static(
        "network_inventory",
        lambda: _run_powershell_json(
            "Get-CimInstance Win32_NetworkAdapter -Filter 'NetEnabled=True' | "
            "Select-Object Name,NetConnectionID,Description,AdapterType,Speed,MACAddress,PhysicalAdapter",
            timeout=5,
        ),
    ))
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in (_clean_text(row.get("NetConnectionID")), _clean_text(row.get("Name"))):
            if key:
                result[key.lower()] = row
    return result


def _collect_wifi_details() -> dict[str, dict[str, Any]]:
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=creationflags,
        )
    except Exception:
        return {}

    if result.returncode != 0 or not result.stdout.strip():
        return {}

    details: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] = {}
    for raw_line in result.stdout.splitlines():
        if ":" not in raw_line:
            continue
        key, value = [part.strip() for part in raw_line.split(":", 1)]
        lower_key = key.lower()
        if lower_key == "name":
            if current.get("name"):
                details[str(current["name"]).lower()] = current
            current = {"name": value}
        elif lower_key == "ssid" and not lower_key.endswith("bssid"):
            current["ssid"] = value
        elif lower_key == "signal":
            percent = _to_int(value.replace("%", ""))
            if percent is not None:
                current["signalStrengthDbm"] = int((percent / 2) - 100)

    if current.get("name"):
        details[str(current["name"]).lower()] = current
    return details


def _collect_networks() -> list[dict[str, Any]]:
    networks: list[dict[str, Any]] = []
    stats = psutil.net_if_stats()
    inventory = _collect_network_inventory()
    wifi_details = _collect_wifi_details()
    for name, addresses in psutil.net_if_addrs().items():
        if _is_loopback(name):
            continue
        stat = stats.get(name)
        if stat and not stat.isup:
            continue

        ipv4 = ""
        ipv6 = ""
        for address in addresses:
            if address.family == socket.AF_INET:
                ipv4 = address.address
            elif address.family == socket.AF_INET6:
                ipv6 = address.address

        if not ipv4 and not ipv6:
            continue

        nic_info = inventory.get(name.lower(), {})
        wifi_info = wifi_details.get(name.lower(), {})
        connection_type = _connection_type(
            " ".join(filter(None, [
                name,
                _clean_text(nic_info.get("Name")),
                _clean_text(nic_info.get("AdapterType")),
                _clean_text(nic_info.get("Description")),
            ]))
        )

        networks.append({
            "adapterName": name,
            "connectionType": connection_type,
            "ipv4": ipv4,
            "ipv6": ipv6,
            "model": _clean_text(nic_info.get("Description")) or _clean_text(nic_info.get("Name")),
            "speedBitsPerSecond": _to_int(nic_info.get("Speed")),
            "macAddress": _clean_text(nic_info.get("MACAddress")),
            "ssid": wifi_info.get("ssid"),
            "signalStrengthDbm": wifi_info.get("signalStrengthDbm"),
        })
    return networks


def _extract_counter_values(value: Any) -> list[int]:
    values = []
    for row in _as_list(value):
        if not isinstance(row, dict):
            continue
        value = _to_int(row.get("CookedValue"))
        if value is not None:
            values.append(value)
    return values


def _empty_gpu_counters() -> dict[str, Any]:
    return {"dedicated": [], "shared": [], "usagePercent": None}


def _read_gpu_counters() -> dict[str, Any]:
    if native_gpu_usage is not None:
        memory = native_gpu_usage.read_memory_usage_bytes()
        usage = native_gpu_usage.read_usage_percent()
        dedicated = memory.get("dedicated", []) if isinstance(memory, dict) else []
        shared = memory.get("shared", []) if isinstance(memory, dict) else []
        if dedicated or shared or usage is not None:
            return {
                "dedicated": dedicated,
                "shared": shared,
                "usagePercent": usage,
            }

    data = _run_powershell_json(
        "$dedicated = @(Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples | Select-Object Path,CookedValue; "
        "$shared = @(Get-Counter '\\GPU Adapter Memory(*)\\Shared Usage' -ErrorAction SilentlyContinue).CounterSamples | Select-Object Path,CookedValue; "
        "$samples = @(Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples; "
        "$groups = @{}; "
        "foreach ($sample in $samples) { "
        "$key = ([string]$sample.InstanceName).ToLower() -replace '^pid_[^_]+_', ''; "
        "if (-not $groups.ContainsKey($key)) { $groups[$key] = 0.0 }; "
        "$groups[$key] += [Math]::Max(0.0, [double]$sample.CookedValue) "
        "}; "
        "$value = 0.0; "
        "foreach ($entry in $groups.GetEnumerator()) { $value = [Math]::Max($value, [Math]::Min([double]$entry.Value, 100.0)) }; "
        "[pscustomobject]@{Dedicated=$dedicated; Shared=$shared; Usage=[Math]::Round([Math]::Min([double]$value, 100), 1)}",
        timeout=8,
    )
    if isinstance(data, dict):
        usage = None
        try:
            usage = max(0.0, min(float(data.get("Usage")), 100.0))
        except (TypeError, ValueError):
            usage = None
        return {
            "dedicated": _extract_counter_values(data.get("Dedicated")),
            "shared": _extract_counter_values(data.get("Shared")),
            "usagePercent": usage,
        }
    return _empty_gpu_counters()


def _refresh_gpu_counters() -> None:
    global _gpu_counter_cache, _gpu_counter_cache_time, _gpu_counter_refreshing

    try:
        counters = _read_gpu_counters()
        with _gpu_counter_lock:
            _gpu_counter_cache = counters
            _gpu_counter_cache_time = time.time()
    finally:
        with _gpu_counter_lock:
            _gpu_counter_refreshing = False


def _schedule_gpu_counter_refresh() -> None:
    global _gpu_counter_refreshing

    with _gpu_counter_lock:
        if _gpu_counter_refreshing:
            return
        _gpu_counter_refreshing = True

    thread = threading.Thread(target=_refresh_gpu_counters, name="pm-gpu-counters", daemon=True)
    thread.start()


def _collect_gpu_counters() -> dict[str, Any]:
    now = time.time()
    with _gpu_counter_lock:
        cached = copy.deepcopy(_gpu_counter_cache)
        cache_time = _gpu_counter_cache_time

    if cached is None or now - cache_time >= GPU_COUNTER_CACHE_SECONDS:
        _schedule_gpu_counter_refresh()

    return cached if cached is not None else _empty_gpu_counters()


def _normalize_gpu_name(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _collect_wmi_gpu_inventory() -> list[dict[str, Any]]:
    rows = _as_list(_cached_static(
        "gpu_inventory_wmi",
        lambda: _run_powershell_json(
            "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,DriverDate,AdapterRAM"
        ),
    ))
    return [row for row in rows if isinstance(row, dict)]


def _collect_dxgi_gpu_inventory() -> list[dict[str, Any]]:
    if native_gpu_inventory is None:
        return []
    rows = _cached_static("gpu_inventory_dxgi", native_gpu_inventory.read_gpu_inventory)
    return [row for row in _as_list(rows) if isinstance(row, dict)]


def _parse_mb_text(value: Any) -> int | None:
    if value is None:
        return None
    first = str(value).strip().split(" ", 1)[0].replace(",", "")
    parsed = _to_int(first)
    return parsed * 1024 * 1024 if parsed is not None else None


def _collect_dxdiag_info() -> dict[str, Any]:
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW

    path = os.path.join(tempfile.gettempdir(), f"processmanager-dxdiag-{os.getpid()}.xml")
    try:
        if os.path.exists(path):
            os.remove(path)
        subprocess.run(
            ["dxdiag", "/whql:off", "/x", path],
            capture_output=True,
            timeout=20,
            creationflags=creationflags,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                break
            time.sleep(0.2)
        if not os.path.exists(path) or os.path.getsize(path) <= 0:
            return {}

        root = ET.parse(path).getroot()
        system = root.find("SystemInformation")
        devices = []
        seen_names: set[str] = set()
        for device in root.findall(".//DisplayDevice"):
            name = _clean_text(device.findtext("CardName"))
            if not name:
                continue
            normalized = _normalize_gpu_name(name)
            if normalized in seen_names:
                continue
            seen_names.add(normalized)
            devices.append({
                "model": name,
                "driverVersion": _clean_text(device.findtext("DriverVersion")),
                "driverDate": _clean_text(device.findtext("DriverDate")),
                "directXVersion": _clean_text(system.findtext("DirectXVersion")) if system is not None else None,
                "ddiVersion": _clean_text(device.findtext("DDIVersion")),
                "featureLevels": _clean_text(device.findtext("FeatureLevels")),
                "driverModel": _clean_text(device.findtext("DriverModel")),
                "displayMemoryBytes": _parse_mb_text(device.findtext("DisplayMemory")),
                "dedicatedMemoryBytes": _parse_mb_text(device.findtext("DedicatedMemory")),
                "sharedMemoryTotalBytes": _parse_mb_text(device.findtext("SharedMemory")),
            })
        return {
            "directXVersion": _clean_text(system.findtext("DirectXVersion")) if system is not None else None,
            "displayDevices": devices,
        }
    except Exception:
        return {}
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _get_dxdiag_info() -> dict[str, Any]:
    return _cached_static("gpu_dxdiag", _collect_dxdiag_info)


def _match_by_model(model: Any, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized_model = _normalize_gpu_name(model)
    for row in rows:
        row_model = _normalize_gpu_name(row.get("model") or row.get("Name"))
        if normalized_model and row_model and (
            normalized_model == row_model or normalized_model in row_model or row_model in normalized_model
        ):
            return row
    return None


def _collect_gpu_temperatures() -> list[dict[str, Any]]:
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            creationflags=creationflags,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    rows = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        rows.append({
            "model": parts[0],
            "temperatureCelsius": _to_int(parts[1]),
        })
    return rows


def _refresh_gpu_temperatures() -> None:
    global _gpu_temperature_cache, _gpu_temperature_cache_time, _gpu_temperature_refreshing

    try:
        temperatures = _collect_gpu_temperatures()
        with _gpu_temperature_lock:
            _gpu_temperature_cache = temperatures
            _gpu_temperature_cache_time = time.time()
    finally:
        with _gpu_temperature_lock:
            _gpu_temperature_refreshing = False


def _schedule_gpu_temperature_refresh() -> None:
    global _gpu_temperature_refreshing

    with _gpu_temperature_lock:
        if _gpu_temperature_refreshing:
            return
        _gpu_temperature_refreshing = True

    thread = threading.Thread(target=_refresh_gpu_temperatures, name="pm-gpu-temperature", daemon=True)
    thread.start()


def _get_gpu_temperatures() -> list[dict[str, Any]]:
    now = time.time()
    with _gpu_temperature_lock:
        cached = copy.deepcopy(_gpu_temperature_cache)
        cache_time = _gpu_temperature_cache_time

    if cached is None or now - cache_time >= GPU_TEMPERATURE_CACHE_SECONDS:
        _schedule_gpu_temperature_refresh()

    return cached if cached is not None else []


def _merge_gpu_inventory(dxgi_rows: list[dict[str, Any]], wmi_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not dxgi_rows:
        return [{**row, "source": "wmi"} for row in wmi_rows]

    used_wmi_indexes: set[int] = set()
    merged: list[dict[str, Any]] = []
    for dxgi_row in dxgi_rows:
        dxgi_name = _normalize_gpu_name(dxgi_row.get("model"))
        match = None
        for index, wmi_row in enumerate(wmi_rows):
            if index in used_wmi_indexes:
                continue
            wmi_name = _normalize_gpu_name(wmi_row.get("Name"))
            if dxgi_name and wmi_name and (dxgi_name == wmi_name or dxgi_name in wmi_name or wmi_name in dxgi_name):
                match = (index, wmi_row)
                break

        next_row = dict(dxgi_row)
        if match:
            used_wmi_indexes.add(match[0])
            next_row["driverVersion"] = _clean_text(match[1].get("DriverVersion"))
            next_row["driverDate"] = _clean_text(match[1].get("DriverDate"))
            next_row["wmiAdapterRamBytes"] = _first_number(match[1].get("AdapterRAM"))
        merged.append(next_row)

    return merged


def _collect_gpu_inventory() -> list[dict[str, Any]]:
    wmi_rows = _collect_wmi_gpu_inventory()
    return _merge_gpu_inventory(_collect_dxgi_gpu_inventory(), wmi_rows)


def _collect_gpus() -> list[dict[str, Any]]:
    rows = _collect_gpu_inventory()
    gpus: list[dict[str, Any]] = []
    counters = _collect_gpu_counters()
    dedicated_usage = counters["dedicated"]
    shared_usage = counters["shared"]
    usage_percent = counters["usagePercent"]
    memory_total = psutil.virtual_memory().total
    shared_memory_total = memory_total // 2 if memory_total else None
    dxdiag_info = _get_dxdiag_info()
    dxdiag_devices = [row for row in _as_list(dxdiag_info.get("displayDevices")) if isinstance(row, dict)]
    temperatures = _get_gpu_temperatures()
    temperature_rows = [row for row in _as_list(temperatures) if isinstance(row, dict)]

    for row in rows:
        if not isinstance(row, dict):
            continue
        index = len(gpus)
        dxdiag = _match_by_model(row.get("model") or row.get("Name"), dxdiag_devices) or {}
        temperature = _match_by_model(row.get("model") or row.get("Name"), temperature_rows)
        memory = _first_number(row.get("dedicatedMemoryBytes"), dxdiag.get("dedicatedMemoryBytes"), row.get("AdapterRAM"))
        dedicated_system = _first_number(row.get("dedicatedSystemMemoryBytes"))
        hardware_reserved = dedicated_system if dedicated_system is not None else None
        row_shared_total = _first_number(row.get("sharedMemoryTotalBytes"), dxdiag.get("sharedMemoryTotalBytes")) or shared_memory_total
        dedicated_used = dedicated_usage[index] if index < len(dedicated_usage) else None
        shared_used = shared_usage[index] if index < len(shared_usage) else None
        gpu_memory_used_parts = [value for value in (dedicated_used, shared_used) if value is not None]
        gpu_memory_total_parts = [value for value in (memory, dedicated_system, row_shared_total) if value is not None]
        gpu_memory_used = sum(gpu_memory_used_parts) if gpu_memory_used_parts else None
        gpu_memory_total = sum(gpu_memory_total_parts) if gpu_memory_total_parts else None
        gpus.append({
            "model": _clean_text(row.get("model")) or _clean_text(row.get("Name")),
            "driverVersion": (
                _clean_text(row.get("driverVersion"))
                or _clean_text(row.get("DriverVersion"))
                or dxdiag.get("driverVersion")
            ),
            "source": _clean_text(row.get("source")),
            "vendorId": row.get("vendorId"),
            "deviceId": row.get("deviceId"),
            "adapterLuid": row.get("adapterLuid"),
            "dedicatedMemoryBytes": memory,
            "usedMemoryBytes": dedicated_used,
            "sharedMemoryBytes": shared_used,
            "gpuMemoryUsedBytes": gpu_memory_used,
            "gpuMemoryTotalBytes": gpu_memory_total,
            "dedicatedMemoryUsedBytes": dedicated_used,
            "dedicatedMemoryTotalBytes": memory,
            "dedicatedSystemMemoryBytes": dedicated_system,
            "hardwareReservedMemoryBytes": hardware_reserved,
            "sharedMemoryUsedBytes": shared_used,
            "sharedMemoryTotalBytes": row_shared_total,
            "displayMemoryBytes": dxdiag.get("displayMemoryBytes"),
            "temperatureCelsius": temperature.get("temperatureCelsius") if temperature else None,
            "driverDate": dxdiag.get("driverDate") or row.get("driverDate") or row.get("DriverDate"),
            "directXVersion": dxdiag.get("directXVersion") or dxdiag_info.get("directXVersion"),
            "ddiVersion": dxdiag.get("ddiVersion"),
            "featureLevels": dxdiag.get("featureLevels"),
            "driverModel": dxdiag.get("driverModel"),
            "usagePercent": usage_percent,
        })
    return gpus


def _disk_groups(disks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = []
    for index, disk in enumerate(disks):
        title = disk.get("mountpoint") or disk.get("device") or f"disk-{index + 1}"
        groups.append({
            "key": f"disk.{index}",
            "titleValue": title,
            "items": [
                _item("mountpoint", disk.get("mountpoint")),
                _item("partitions", disk.get("partitions")),
                _item("device", disk.get("device")),
                _item("filesystem", disk.get("fstype")),
                _item("totalBytes", disk.get("totalBytes"), "bytes"),
                _item("usedBytes", disk.get("usedBytes"), "bytes"),
                _item("freeBytes", disk.get("freeBytes"), "bytes"),
                _item("usagePercent", disk.get("usagePercent"), "percent"),
                _item("activeTimePercent", disk.get("activeTimePercent"), "percent"),
                _item("capacityUsagePercent", disk.get("capacityUsagePercent"), "percent"),
                _item("readBytesPerSecond", disk.get("readBytesPerSecond"), "bytesPerSecond"),
                _item("writeBytesPerSecond", disk.get("writeBytesPerSecond"), "bytesPerSecond"),
                _item("averageResponseTimeMs", disk.get("averageResponseTimeMs"), "ms"),
                _item("queueLength", disk.get("queueLength"), "count"),
                _item("diskType", disk.get("type")),
                _item("model", disk.get("model")),
            ],
        })
    return groups


def _network_groups(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = []
    for index, network in enumerate(networks):
        title = network.get("adapterName") or f"network-{index + 1}"
        groups.append({
            "key": f"network.{index}",
            "titleValue": title,
            "items": [
                _item("adapterName", network.get("adapterName")),
                _item("connectionType", network.get("connectionType")),
                _item("ipv4", network.get("ipv4")),
                _item("ipv6", network.get("ipv6")),
                _item("speedBitsPerSecond", network.get("speedBitsPerSecond"), "bitsPerSecond"),
                _item("macAddress", network.get("macAddress")),
                _item("model", network.get("model")),
                _item("ssid", network.get("ssid")),
                _item("signalStrengthDbm", network.get("signalStrengthDbm"), "dbm"),
            ],
        })
    return groups


def _gpu_groups(gpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = []
    for index, gpu in enumerate(gpus):
        title = gpu.get("model") or f"gpu-{index + 1}"
        groups.append({
            "key": f"gpu.{index}",
            "titleValue": title,
            "items": [
                _item("model", gpu.get("model")),
                _item("driverVersion", gpu.get("driverVersion")),
                _item("dedicatedMemoryBytes", gpu.get("dedicatedMemoryBytes"), "bytes"),
                _item("usedMemoryBytes", gpu.get("usedMemoryBytes"), "bytes"),
                _item("sharedMemoryBytes", gpu.get("sharedMemoryBytes"), "bytes"),
                _item("gpuMemoryUsedBytes", gpu.get("gpuMemoryUsedBytes"), "bytes"),
                _item("gpuMemoryTotalBytes", gpu.get("gpuMemoryTotalBytes"), "bytes"),
                _item("dedicatedMemoryUsedBytes", gpu.get("dedicatedMemoryUsedBytes"), "bytes"),
                _item("dedicatedMemoryTotalBytes", gpu.get("dedicatedMemoryTotalBytes"), "bytes"),
                _item("dedicatedSystemMemoryBytes", gpu.get("dedicatedSystemMemoryBytes"), "bytes"),
                _item("hardwareReservedMemoryBytes", gpu.get("hardwareReservedMemoryBytes"), "bytes"),
                _item("sharedMemoryUsedBytes", gpu.get("sharedMemoryUsedBytes"), "bytes"),
                _item("sharedMemoryTotalBytes", gpu.get("sharedMemoryTotalBytes"), "bytes"),
                _item("displayMemoryBytes", gpu.get("displayMemoryBytes"), "bytes"),
                _item("temperatureCelsius", gpu.get("temperatureCelsius"), "celsius"),
                _item("driverDate", gpu.get("driverDate")),
                _item("directXVersion", gpu.get("directXVersion")),
                _item("ddiVersion", gpu.get("ddiVersion")),
                _item("featureLevels", gpu.get("featureLevels")),
                _item("driverModel", gpu.get("driverModel")),
                _item("usagePercent", gpu.get("usagePercent"), "percent"),
            ],
        })
    return groups


def _sections(cpu: dict[str, Any], memory: dict[str, Any], disks: list[dict[str, Any]], networks: list[dict[str, Any]], gpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _section("windows.system", [
            _item("hostname", socket.gethostname()),
            _item("kernelSystem", platform.system()),
            _item("kernelRelease", platform.release()),
            _item("kernelVersion", platform.version()),
            _item("architecture", platform.machine()),
            _item("bootTimeEpochSeconds", int(psutil.boot_time()), "epochSeconds"),
            _item("uptimeSeconds", cpu.get("uptimeSeconds"), "seconds"),
        ]),
        _section("windows.cpu", [
            _item("model", cpu.get("model")),
            _item("sockets", cpu.get("sockets"), "count"),
            _item("cores", cpu.get("cores"), "count"),
            _item("logicalProcessors", cpu.get("logicalProcessors"), "count"),
            _item("baseSpeedMhz", cpu.get("baseSpeedMhz"), "mhz"),
            _item("currentSpeedMhz", cpu.get("currentSpeedMhz"), "mhz"),
            _item("virtualization", cpu.get("virtualization")),
            _item("l2CacheBytes", cpu.get("l2CacheBytes"), "bytes"),
            _item("l3CacheBytes", cpu.get("l3CacheBytes"), "bytes"),
        ]),
        _section("windows.memory", [
            _item("totalBytes", memory.get("totalBytes"), "bytes"),
            _item("usedBytes", memory.get("usedBytes"), "bytes"),
            _item("availableBytes", memory.get("availableBytes"), "bytes"),
            _item("cachedBytes", memory.get("cachedBytes"), "bytes"),
            _item("committedBytes", memory.get("committedBytes"), "bytes"),
            _item("commitLimitBytes", memory.get("commitLimitBytes"), "bytes"),
            _item("pagedPoolBytes", memory.get("pagedPoolBytes"), "bytes"),
            _item("nonPagedPoolBytes", memory.get("nonPagedPoolBytes"), "bytes"),
            _item("hardwareReservedBytes", memory.get("hardwareReservedBytes"), "bytes"),
            _item("usagePercent", memory.get("usagePercent"), "percent"),
            _item("installedBytes", memory.get("installedBytes"), "bytes"),
            _item("speedMtPerSecond", memory.get("speedMtPerSecond"), "mtPerSecond"),
            _item("slotsUsed", memory.get("slotsUsed"), "count"),
            _item("slotsTotal", memory.get("slotsTotal"), "count"),
            _item("formFactor", memory.get("formFactor")),
        ]),
        _section("windows.disks", [], _disk_groups(disks)),
        _section("windows.networks", [], _network_groups(networks)),
        _section("windows.gpus", [], _gpu_groups(gpus)),
    ]


def _future_result(future: Future, fallback: Any) -> Any:
    try:
        return future.result()
    except Exception:
        return fallback


def _build_hardware_payload(
    cpu: dict[str, Any],
    memory: dict[str, Any],
    disks: list[dict[str, Any]],
    networks: list[dict[str, Any]],
    gpus: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "osType": "Windows",
        "capabilities": WINDOWS_CAPABILITIES,
        "summary": {
            "cpu": {
                "model": cpu.get("model"),
                "cores": cpu.get("cores"),
                "logicalProcessors": cpu.get("logicalProcessors"),
                "baseSpeedMhz": cpu.get("baseSpeedMhz"),
                "uptimeSeconds": cpu.get("uptimeSeconds"),
            },
            "memory": {
                "totalBytes": memory.get("totalBytes"),
                "usedBytes": memory.get("usedBytes"),
                "availableBytes": memory.get("availableBytes"),
                "usagePercent": memory.get("usagePercent"),
                "cachedBytes": memory.get("cachedBytes"),
                "committedBytes": memory.get("committedBytes"),
                "commitLimitBytes": memory.get("commitLimitBytes"),
                "pagedPoolBytes": memory.get("pagedPoolBytes"),
                "nonPagedPoolBytes": memory.get("nonPagedPoolBytes"),
                "hardwareReservedBytes": memory.get("hardwareReservedBytes"),
                "installedBytes": memory.get("installedBytes"),
                "speedMtPerSecond": memory.get("speedMtPerSecond"),
                "slotsUsed": memory.get("slotsUsed"),
                "slotsTotal": memory.get("slotsTotal"),
                "formFactor": memory.get("formFactor"),
            },
            "disks": [
                {
                    "mountpoint": disk.get("mountpoint"),
                    "partitions": disk.get("partitions"),
                    "device": disk.get("device"),
                    "model": disk.get("model"),
                    "type": disk.get("type"),
                    "totalBytes": disk.get("totalBytes"),
                    "usedBytes": disk.get("usedBytes"),
                    "freeBytes": disk.get("freeBytes"),
                    "usagePercent": disk.get("usagePercent"),
                    "activeTimePercent": disk.get("activeTimePercent"),
                    "capacityUsagePercent": disk.get("capacityUsagePercent"),
                    "readBytesPerSecond": disk.get("readBytesPerSecond"),
                    "writeBytesPerSecond": disk.get("writeBytesPerSecond"),
                    "averageResponseTimeMs": disk.get("averageResponseTimeMs"),
                    "queueLength": disk.get("queueLength"),
                }
                for disk in disks
            ],
            "networks": [
                {
                    "adapterName": network.get("adapterName"),
                    "ipv4": network.get("ipv4"),
                    "ipv6": network.get("ipv6"),
                    "connectionType": network.get("connectionType"),
                    "model": network.get("model"),
                    "speedBitsPerSecond": network.get("speedBitsPerSecond"),
                    "macAddress": network.get("macAddress"),
                    "ssid": network.get("ssid"),
                    "signalStrengthDbm": network.get("signalStrengthDbm"),
                }
                for network in networks
            ],
            "gpus": [
                {
                    "model": gpu.get("model"),
                    "driverVersion": gpu.get("driverVersion"),
                    "gpuMemoryUsedBytes": gpu.get("gpuMemoryUsedBytes"),
                    "gpuMemoryTotalBytes": gpu.get("gpuMemoryTotalBytes"),
                    "dedicatedMemoryUsedBytes": gpu.get("dedicatedMemoryUsedBytes"),
                    "dedicatedMemoryTotalBytes": gpu.get("dedicatedMemoryTotalBytes"),
                    "dedicatedSystemMemoryBytes": gpu.get("dedicatedSystemMemoryBytes"),
                    "hardwareReservedMemoryBytes": gpu.get("hardwareReservedMemoryBytes"),
                    "sharedMemoryUsedBytes": gpu.get("sharedMemoryUsedBytes"),
                    "sharedMemoryTotalBytes": gpu.get("sharedMemoryTotalBytes"),
                    "displayMemoryBytes": gpu.get("displayMemoryBytes"),
                    "temperatureCelsius": gpu.get("temperatureCelsius"),
                    "driverDate": gpu.get("driverDate"),
                    "directXVersion": gpu.get("directXVersion"),
                    "ddiVersion": gpu.get("ddiVersion"),
                    "featureLevels": gpu.get("featureLevels"),
                    "driverModel": gpu.get("driverModel"),
                    "usagePercent": gpu.get("usagePercent"),
                }
                for gpu in gpus
            ],
        },
        "cpu": cpu,
        "memory": memory,
        "disks": disks,
        "networks": networks,
        "gpus": gpus,
        "sections": _sections(cpu, memory, disks, networks, gpus),
    }


def _collect_hardware_snapshot() -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=5) as executor:
        cpu_future = executor.submit(_collect_cpu)
        memory_future = executor.submit(_collect_memory)
        disks_future = executor.submit(_collect_disks)
        networks_future = executor.submit(_collect_networks)
        gpus_future = executor.submit(_collect_gpus)

        cpu = _future_result(cpu_future, {})
        memory = _future_result(memory_future, {})
        disks = _future_result(disks_future, [])
        networks = _future_result(networks_future, [])
        gpus = _future_result(gpus_future, [])

    return _build_hardware_payload(cpu, memory, disks, networks, gpus)


def _collect_cpu_quick() -> dict[str, Any]:
    freq = psutil.cpu_freq()
    perf = native_cpu_perf.read_cpu_perf() if native_cpu_perf is not None else {}
    return {
        "model": platform.processor() or os.getenv("PROCESSOR_IDENTIFIER") or None,
        "sockets": 1,
        "cores": psutil.cpu_count(logical=False) or 1,
        "logicalProcessors": psutil.cpu_count(logical=True) or 1,
        "baseSpeedMhz": round(freq.max, 1) if freq and freq.max else None,
        "currentSpeedMhz": _first_available(
            perf.get("currentSpeedMhz"),
            round(freq.current, 1) if freq else None,
        ),
        "virtualization": None,
        "l2CacheBytes": None,
        "l3CacheBytes": None,
        "uptimeSeconds": int(time.time() - psutil.boot_time()),
    }


def _collect_memory_quick() -> dict[str, Any]:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    perf = _collect_memory_perf()
    physical = _get_cached_static("physical_memory")

    result = {
        "totalBytes": mem.total,
        "usedBytes": mem.used,
        "availableBytes": mem.available,
        "usagePercent": round(mem.percent, 1),
        "cachedBytes": perf.get("CacheBytes", getattr(mem, "cached", 0) or 0),
        "committedBytes": perf.get("CommittedBytes", mem.used + getattr(swap, "used", 0)),
        "commitLimitBytes": perf.get("CommitLimit", mem.total + getattr(swap, "total", 0)),
        "pagedPoolBytes": perf.get("PoolPagedBytes"),
        "nonPagedPoolBytes": perf.get("PoolNonpagedBytes"),
        "hardwareReservedBytes": None,
    }
    if physical is not None:
        result.update(_collect_physical_memory())
    return result


def _collect_disks_quick() -> list[dict[str, Any]]:
    if _get_cached_static("disk_inventory") is not None:
        return _collect_disks()

    system_drive = (os.getenv("SystemDrive") or "C:").rstrip("\\")
    system_mount = f"{system_drive}\\"
    disks = []
    for partition in psutil.disk_partitions(all=False):
        if partition.mountpoint.rstrip("\\").lower() != system_mount.rstrip("\\").lower():
            continue
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue

        disks.append({
            "mountpoint": partition.mountpoint,
            "partitions": partition.mountpoint,
            "device": partition.device,
            "fstype": partition.fstype,
            "totalBytes": usage.total,
            "usedBytes": usage.used,
            "freeBytes": usage.free,
            "usagePercent": round(usage.percent, 1),
            "activeTimePercent": None,
            "capacityUsagePercent": round(usage.percent, 1),
            "readBytesPerSecond": None,
            "writeBytesPerSecond": None,
            "averageResponseTimeMs": None,
            "queueLength": None,
            "type": None,
            "model": None,
        })
    return disks


def _collect_networks_quick() -> list[dict[str, Any]]:
    networks = []
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if _is_loopback(name):
            continue
        stat = stats.get(name)
        if stat and not stat.isup:
            continue

        ipv4 = ""
        ipv6 = ""
        for address in addresses:
            if address.family == socket.AF_INET:
                ipv4 = address.address
            elif address.family == socket.AF_INET6:
                ipv6 = address.address

        if not ipv4 and not ipv6:
            continue

        networks.append({
            "adapterName": name,
            "connectionType": _connection_type(name),
            "ipv4": ipv4,
            "ipv6": ipv6,
            "model": None,
            "speedBitsPerSecond": None,
            "macAddress": None,
            "ssid": None,
            "signalStrengthDbm": None,
        })
    return networks


def _collect_gpus_quick() -> list[dict[str, Any]]:
    dxgi_rows = [row for row in _as_list(_get_cached_static("gpu_inventory_dxgi")) if isinstance(row, dict)]
    wmi_rows = [row for row in _as_list(_get_cached_static("gpu_inventory_wmi")) if isinstance(row, dict)]
    rows = _merge_gpu_inventory(dxgi_rows, wmi_rows)
    gpus = []
    memory_total = psutil.virtual_memory().total
    shared_memory_total = memory_total // 2 if memory_total else None
    dxdiag_info = _get_cached_static("gpu_dxdiag") or {}
    dxdiag_devices = [row for row in _as_list(dxdiag_info.get("displayDevices")) if isinstance(row, dict)]
    temperature_rows = [row for row in _as_list(_get_gpu_temperatures()) if isinstance(row, dict)]
    for row in rows:
        if not isinstance(row, dict):
            continue
        dxdiag = _match_by_model(row.get("model") or row.get("Name"), dxdiag_devices) or {}
        temperature = _match_by_model(row.get("model") or row.get("Name"), temperature_rows)
        dedicated_total = _first_number(row.get("dedicatedMemoryBytes"), dxdiag.get("dedicatedMemoryBytes"), row.get("AdapterRAM"))
        dedicated_system = _first_number(row.get("dedicatedSystemMemoryBytes"))
        hardware_reserved = dedicated_system if dedicated_system is not None else None
        row_shared_total = _first_number(row.get("sharedMemoryTotalBytes"), dxdiag.get("sharedMemoryTotalBytes")) or shared_memory_total
        gpu_memory_total_parts = [
            value for value in (dedicated_total, dedicated_system, row_shared_total) if value is not None
        ]
        gpus.append({
            "model": _clean_text(row.get("model")) or _clean_text(row.get("Name")),
            "driverVersion": (
                _clean_text(row.get("driverVersion"))
                or _clean_text(row.get("DriverVersion"))
                or dxdiag.get("driverVersion")
            ),
            "source": _clean_text(row.get("source")),
            "vendorId": row.get("vendorId"),
            "deviceId": row.get("deviceId"),
            "adapterLuid": row.get("adapterLuid"),
            "dedicatedMemoryBytes": dedicated_total,
            "usedMemoryBytes": None,
            "sharedMemoryBytes": None,
            "gpuMemoryUsedBytes": None,
            "gpuMemoryTotalBytes": sum(gpu_memory_total_parts) if gpu_memory_total_parts else None,
            "dedicatedMemoryUsedBytes": None,
            "dedicatedMemoryTotalBytes": dedicated_total,
            "dedicatedSystemMemoryBytes": dedicated_system,
            "hardwareReservedMemoryBytes": hardware_reserved,
            "sharedMemoryUsedBytes": None,
            "sharedMemoryTotalBytes": row_shared_total,
            "displayMemoryBytes": dxdiag.get("displayMemoryBytes"),
            "temperatureCelsius": temperature.get("temperatureCelsius") if temperature else None,
            "driverDate": dxdiag.get("driverDate") or row.get("driverDate") or row.get("DriverDate"),
            "directXVersion": dxdiag.get("directXVersion") or dxdiag_info.get("directXVersion"),
            "ddiVersion": dxdiag.get("ddiVersion"),
            "featureLevels": dxdiag.get("featureLevels"),
            "driverModel": dxdiag.get("driverModel"),
            "usagePercent": None,
        })
    return gpus


def _collect_quick_hardware_snapshot() -> dict[str, Any]:
    snapshot = _build_hardware_payload(
        _collect_cpu_quick(),
        _collect_memory_quick(),
        _collect_disks_quick(),
        _collect_networks_quick(),
        _collect_gpus_quick(),
    )
    snapshot["snapshotStatus"] = "warming"
    return snapshot


def _store_hardware_snapshot(snapshot: dict[str, Any]) -> None:
    global _latest_hardware_snapshot, _latest_hardware_snapshot_time

    with _latest_hardware_lock:
        _latest_hardware_snapshot = copy.deepcopy(snapshot)
        _latest_hardware_snapshot_time = time.time()
    _first_snapshot_event.set()


def _get_hardware_snapshot(max_age_seconds: float | None = None) -> dict[str, Any] | None:
    with _latest_hardware_lock:
        if _latest_hardware_snapshot is None:
            return None
        if max_age_seconds is not None and time.time() - _latest_hardware_snapshot_time > max_age_seconds:
            return None
        return copy.deepcopy(_latest_hardware_snapshot)


def _hardware_sampler_loop() -> None:
    while True:
        started_at = time.perf_counter()
        try:
            _store_hardware_snapshot(_collect_hardware_snapshot())
        except Exception:
            pass

        elapsed = time.perf_counter() - started_at
        time.sleep(max(0.0, HARDWARE_SAMPLER_INTERVAL_SECONDS - elapsed))


def start_hardware_sampler() -> None:
    global _sampler_started

    with _sampler_lock:
        if _sampler_started:
            return
        _sampler_started = True

    thread = threading.Thread(target=_hardware_sampler_loop, name="pm-hardware-sampler", daemon=True)
    thread.start()


def warm_hardware_cache() -> None:
    start_hardware_sampler()


def collect_hardware() -> dict[str, Any]:
    start_hardware_sampler()
    snapshot = _get_hardware_snapshot(HARDWARE_SNAPSHOT_STALE_SECONDS)
    if snapshot is not None:
        return snapshot

    if _first_snapshot_event.wait(timeout=0.1):
        snapshot = _get_hardware_snapshot(HARDWARE_SNAPSHOT_STALE_SECONDS)
        if snapshot is not None:
            return snapshot

    snapshot = _collect_quick_hardware_snapshot()
    _store_hardware_snapshot(snapshot)
    return snapshot

"""Windows system metric collection."""
from __future__ import annotations

import os
import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

try:
    from pm_agent.platforms.windows import native_gpu_usage
except Exception:
    native_gpu_usage = None

try:
    from pm_agent.platforms.windows import native_memory_perf
except Exception:
    native_memory_perf = None

try:
    from pm_agent.platforms.windows import hardware as hardware_info
except Exception:
    hardware_info = None

METRIC_DEFINITIONS = {
    1: ("cpu.usagePercent", "percent"),
    2: ("gpu.usagePercent", "percent"),
    3: ("memory.usagePercent", "percent"),
    4: ("disk.usagePercent", "percent"),
    5: ("network.uploadBytesPerSecond", "bytesPerSecond"),
    6: ("network.downloadBytesPerSecond", "bytesPerSecond"),
    7: ("cpu.currentSpeedMhz", "mhz"),
    8: ("memory.usedBytes", "bytes"),
    9: ("memory.availableBytes", "bytes"),
    10: ("memory.cachedBytes", "bytes"),
    11: ("memory.committedBytes", "bytes"),
    12: ("disk.readBytesPerSecond", "bytesPerSecond"),
    13: ("disk.writeBytesPerSecond", "bytesPerSecond"),
    14: ("memory.hardware", "object"),
    15: ("disk.devices", "object"),
}

_last_net_sent: int
_last_net_recv: int
_last_time: float
_last_disk_io = psutil.disk_io_counters()
_last_gpu_usage_value: float | None = None
_last_gpu_usage_time = 0.0
_last_memory_perf: dict[str, int] = {}
_last_memory_perf_time = 0.0
_last_memory_hardware: dict[str, Any] | None = None
_last_memory_hardware_time = 0.0
_last_disk_inventory: list[dict[str, Any]] = []
_last_disk_inventory_time = 0.0

GPU_USAGE_CACHE_SECONDS = 1
POWERSHELL_CACHE_SECONDS = 10
MEMORY_HARDWARE_CACHE_SECONDS = 60
DISK_INVENTORY_CACHE_SECONDS = 60


def _metric(metric_id: int, value: Any) -> dict[str, Any]:
    key, unit = METRIC_DEFINITIONS[metric_id]
    return {
        "id": metric_id,
        "key": key,
        "title": key,
        "value": value,
        "rawValue": value,
        "unit": unit,
        "valueType": "number" if isinstance(value, (int, float)) else "object" if isinstance(value, (dict, list)) else "text",
    }


def _is_loopback_interface(name: str) -> bool:
    normalized = name.strip().lower()
    return (
        normalized == "lo"
        or "loopback" in normalized
        or "localhost" in normalized
        or normalized.startswith("npcap loopback")
    )


def _get_net_io() -> tuple[int, int]:
    sent = 0
    recv = 0
    for nic, counter in psutil.net_io_counters(pernic=True).items():
        if _is_loopback_interface(nic):
            continue
        sent += counter.bytes_sent
        recv += counter.bytes_recv
    return sent, recv


def _get_system_disk_path() -> str:
    system_drive = os.getenv("SystemDrive")
    if system_drive:
        return f"{system_drive}\\"

    home_anchor = Path.home().anchor
    if home_anchor:
        return home_anchor

    return os.getcwd()


def _run_powershell_json(script: str, timeout: int = 4) -> Any:
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


def _to_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _get_gpu_usage() -> float | None:
    global _last_gpu_usage_value, _last_gpu_usage_time

    now = time.time()
    if now - _last_gpu_usage_time < GPU_USAGE_CACHE_SECONDS:
        return _last_gpu_usage_value

    if native_gpu_usage is not None:
        value = native_gpu_usage.read_usage_percent()
        if value is not None:
            _last_gpu_usage_value = value
            _last_gpu_usage_time = now
            return _last_gpu_usage_value

    try:
        import GPUtil

        gpus = GPUtil.getGPUs()
        if gpus:
            _last_gpu_usage_value = round(gpus[0].load * 100, 1)
            _last_gpu_usage_time = now
            return _last_gpu_usage_value
    except Exception:
        pass
    _last_gpu_usage_value = None
    _last_gpu_usage_time = now
    return None


def _get_memory_perf() -> dict[str, int]:
    global _last_memory_perf, _last_memory_perf_time

    now = time.time()
    if now - _last_memory_perf_time < POWERSHELL_CACHE_SECONDS:
        return _last_memory_perf

    next_perf = native_memory_perf.read_memory_perf() if native_memory_perf is not None else {}

    _last_memory_perf = next_perf
    _last_memory_perf_time = now
    return _last_memory_perf


def _memory_type_name(code: Any) -> str | None:
    names = {
        20: "DDR",
        21: "DDR2",
        24: "DDR3",
        26: "DDR4",
        34: "DDR5",
    }
    parsed = _to_int(code)
    return names.get(parsed) if parsed is not None else None


def _get_memory_hardware() -> dict[str, Any] | None:
    global _last_memory_hardware, _last_memory_hardware_time

    now = time.time()
    if now - _last_memory_hardware_time < MEMORY_HARDWARE_CACHE_SECONDS:
        return _last_memory_hardware

    rows = _as_list(_run_powershell_json(
        "Get-CimInstance Win32_PhysicalMemory | "
        "Select-Object Capacity,Speed,ConfiguredClockSpeed,SMBIOSMemoryType",
        timeout=5,
    ))
    modules = [row for row in rows if isinstance(row, dict)]
    if not modules:
        _last_memory_hardware = None
        _last_memory_hardware_time = now
        return None

    capacities = [_to_int(row.get("Capacity")) for row in modules]
    capacities = [capacity for capacity in capacities if capacity is not None]
    speeds = [
        _to_int(row.get("ConfiguredClockSpeed")) or _to_int(row.get("Speed"))
        for row in modules
    ]
    speeds = [speed for speed in speeds if speed is not None]
    memory_types = [_memory_type_name(row.get("SMBIOSMemoryType")) for row in modules]
    memory_types = [memory_type for memory_type in memory_types if memory_type]

    _last_memory_hardware = {
        "slotsUsed": len(modules),
        "perSlotBytes": capacities[0] if capacities else None,
        "totalBytes": sum(capacities) if capacities else None,
        "memoryType": memory_types[0] if memory_types else None,
        "speedMtPerSecond": speeds[0] if speeds else None,
    }
    _last_memory_hardware_time = now
    return _last_memory_hardware


def _get_disk_inventory() -> list[dict[str, Any]]:
    global _last_disk_inventory, _last_disk_inventory_time

    now = time.time()
    if now - _last_disk_inventory_time < DISK_INVENTORY_CACHE_SECONDS:
        return _last_disk_inventory

    if hardware_info is None:
        _last_disk_inventory = []
    else:
        _last_disk_inventory = hardware_info._collect_disk_inventory()
    _last_disk_inventory_time = now
    return _last_disk_inventory


def _get_disk_devices() -> list[dict[str, Any]]:
    if hardware_info is None:
        try:
            usage = psutil.disk_usage(_get_system_disk_path())
            return [{
                "mountpoint": _get_system_disk_path(),
                "partitions": _get_system_disk_path(),
                "device": _get_system_disk_path(),
                "totalBytes": usage.total,
                "usedBytes": usage.used,
                "freeBytes": usage.free,
                "usagePercent": round(usage.percent, 1),
            }]
        except Exception:
            return []

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _get_disk_inventory():
        grouped.setdefault(hardware_info._physical_disk_key(row), []).append(row)

    disks: list[dict[str, Any]] = []
    for rows in grouped.values():
        total = used = free = 0
        mountpoints: list[str] = []
        for row in rows:
            device_id = hardware_info._clean_text(row.get("DeviceId"))
            if not device_id:
                continue
            mountpoint = f"{device_id}\\"
            try:
                usage = psutil.disk_usage(mountpoint)
            except Exception:
                continue
            mountpoints.append(mountpoint)
            total += usage.total
            used += usage.used
            free += usage.free

        if not total:
            continue

        representative = rows[0]
        disk_index = hardware_info._to_int(representative.get("DiskIndex"))
        physical_device = (
            hardware_info._clean_text(representative.get("DiskDeviceId"))
            or (f"PhysicalDrive{disk_index}" if disk_index is not None else None)
        )
        disks.append({
            "mountpoint": ", ".join(hardware_info._unique_text(mountpoints)),
            "partitions": ", ".join(hardware_info._unique_text(mountpoints)),
            "device": physical_device or ", ".join(hardware_info._unique_text([row.get("DeviceId") for row in rows])),
            "totalBytes": total,
            "usedBytes": used,
            "freeBytes": free,
            "usagePercent": round((used / total) * 100, 1),
        })

    return disks


def collect_metrics() -> list[dict[str, Any]]:
    """Collect Windows metrics using the same payload shape as Linux."""
    global _last_net_sent, _last_net_recv, _last_time, _last_disk_io

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu_percent = round(psutil.cpu_percent(interval=None), 1)
    mem_percent = round(mem.percent, 1)

    try:
        disk_percent = round(psutil.disk_usage(_get_system_disk_path()).percent, 1)
    except Exception:
        disk_percent = None

    current_time = time.time()
    time_diff = max(current_time - _last_time, 1)

    cur_sent, cur_recv = _get_net_io()
    sent_bps = int(max(0, (cur_sent - _last_net_sent) / time_diff))
    recv_bps = int(max(0, (cur_recv - _last_net_recv) / time_diff))
    _last_net_sent, _last_net_recv, _last_time = cur_sent, cur_recv, current_time

    freq = psutil.cpu_freq()
    cpu_freq_mhz = round(freq.current, 1) if freq else None

    cur_disk = psutil.disk_io_counters()
    if cur_disk and _last_disk_io:
        disk_read_bps = int(max(0, (cur_disk.read_bytes - _last_disk_io.read_bytes) / time_diff))
        disk_write_bps = int(max(0, (cur_disk.write_bytes - _last_disk_io.write_bytes) / time_diff))
    else:
        disk_read_bps = disk_write_bps = None
    _last_disk_io = cur_disk

    memory_perf = _get_memory_perf()
    cached_bytes = memory_perf.get("CacheBytes", getattr(mem, "cached", 0) or 0)
    committed_bytes = memory_perf.get("CommittedBytes", mem.used + getattr(swap, "used", 0))

    return [
        _metric(1, cpu_percent),
        _metric(2, _get_gpu_usage()),
        _metric(3, mem_percent),
        _metric(4, disk_percent),
        _metric(5, sent_bps),
        _metric(6, recv_bps),
        _metric(7, cpu_freq_mhz),
        _metric(8, mem.used),
        _metric(9, mem.available),
        _metric(10, cached_bytes),
        _metric(11, committed_bytes),
        _metric(12, disk_read_bps),
        _metric(13, disk_write_bps),
        _metric(14, _get_memory_hardware()),
        _metric(15, _get_disk_devices()),
    ]


def get_self_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return ""


_last_net_sent, _last_net_recv = _get_net_io()
_last_time = time.time()

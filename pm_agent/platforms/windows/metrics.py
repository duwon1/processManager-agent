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
    from pm_agent.platforms.windows import native_cpu_perf
except Exception:
    native_cpu_perf = None

try:
    from pm_agent.platforms.windows import native_gpu_usage
except Exception:
    native_gpu_usage = None

try:
    from pm_agent.platforms.windows import native_disk_usage
except Exception:
    native_disk_usage = None

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
    16: ("network.interfaces", "object"),
    17: ("cpu.logicalProcessors", "object"),
}

_last_net_sent: int
_last_net_recv: int
_last_net_interfaces: dict[str, Any]
_last_time: float
_last_disk_io = psutil.disk_io_counters(perdisk=True) or {}
_last_disk_io_time = time.time()
_last_disk_speed_time = 0.0
_last_disk_read_bps: int | None = None
_last_disk_write_bps: int | None = None
_last_disk_speeds: dict[str, tuple[int, int]] = {}
_last_disk_active: dict[str, float] = {}
_last_disk_details: dict[str, dict[str, Any]] = {}
_last_gpu_usage_value: float | None = None
_last_gpu_usage_time = 0.0
_last_memory_perf: dict[str, int] = {}
_last_memory_perf_time = 0.0
_last_memory_hardware: dict[str, Any] | None = None
_last_memory_hardware_time = 0.0
_last_disk_inventory: list[dict[str, Any]] = []
_last_disk_inventory_time = 0.0
_last_cpu_perf: dict[str, float] = {}
_last_cpu_perf_time = 0.0

CPU_PERF_CACHE_SECONDS = 1
GPU_USAGE_CACHE_SECONDS = 1
POWERSHELL_CACHE_SECONDS = 1
# 슬롯/디스크 목록처럼 장치 구성이 바뀌어야 달라지는 값은 실시간 카운터와 분리해 1시간 캐시합니다.
MEMORY_HARDWARE_CACHE_SECONDS = 60 * 60
DISK_INVENTORY_CACHE_SECONDS = 60 * 60
DISK_IO_CACHE_SECONDS = 1


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


def _get_net_io_per_interface() -> dict[str, Any]:
    return {
        nic: counter
        for nic, counter in psutil.net_io_counters(pernic=True).items()
        if not _is_loopback_interface(nic)
    }


def _network_interface_speeds(
    current: dict[str, Any],
    previous: dict[str, Any],
    elapsed: float,
) -> list[dict[str, Any]]:
    networks: list[dict[str, Any]] = []
    for name, counter in current.items():
        before = previous.get(name)
        if before is None:
            sent_bps = None
            recv_bps = None
        else:
            sent_bps = int(max(0, (counter.bytes_sent - before.bytes_sent) / elapsed))
            recv_bps = int(max(0, (counter.bytes_recv - before.bytes_recv) / elapsed))

        networks.append({
            "adapterName": name,
            "sentBytesPerSecond": sent_bps,
            "receivedBytesPerSecond": recv_bps,
        })
    return networks


def _get_cpu_perf(now: float | None = None) -> dict[str, float]:
    global _last_cpu_perf, _last_cpu_perf_time

    current_time = now or time.time()
    if current_time - _last_cpu_perf_time < CPU_PERF_CACHE_SECONDS:
        return _last_cpu_perf

    values = native_cpu_perf.read_cpu_perf() if native_cpu_perf is not None else {}
    _last_cpu_perf = values
    _last_cpu_perf_time = current_time
    return _last_cpu_perf


def _first_available(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


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


def _to_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _disk_io_key(name: Any) -> str:
    return str(name or "").replace("\\\\.\\", "").lower()


def _get_native_disk_io() -> tuple[int | None, int | None, dict[str, tuple[int, int]], dict[str, float], dict[str, dict[str, Any]]] | None:
    if native_disk_usage is None:
        return None

    rows = native_disk_usage.read_disk_counters()
    if not rows:
        return None

    speeds: dict[str, tuple[int, int]] = {}
    active: dict[str, float] = {}
    details: dict[str, dict[str, Any]] = {}
    total_read = 0
    total_write = 0
    for key, values in rows.items():
        read_bps = int(max(0.0, float(values.get("readBytesPerSecond") or 0.0)))
        write_bps = int(max(0.0, float(values.get("writeBytesPerSecond") or 0.0)))
        active_percent = values.get("activeTimePercent")
        speeds[key] = (read_bps, write_bps)
        if active_percent is not None:
            active[key] = max(0.0, min(float(active_percent), 100.0))
        details[key] = {
            "activeTimePercent": active.get(key),
            "averageResponseTimeMs": values.get("averageResponseTimeMs"),
            "queueLength": values.get("queueLength"),
        }
        total_read += read_bps
        total_write += write_bps

    return total_read, total_write, speeds, active, details


def _get_disk_io_speeds(now: float | None = None) -> tuple[int | None, int | None, dict[str, tuple[int, int]], dict[str, float], dict[str, dict[str, Any]]]:
    global _last_disk_io, _last_disk_io_time, _last_disk_speed_time
    global _last_disk_read_bps, _last_disk_write_bps, _last_disk_speeds, _last_disk_active, _last_disk_details

    current_time = now or time.time()
    if current_time - _last_disk_speed_time < DISK_IO_CACHE_SECONDS:
        return _last_disk_read_bps, _last_disk_write_bps, _last_disk_speeds, _last_disk_active, _last_disk_details

    native_sample = _get_native_disk_io()
    if native_sample is not None:
        _last_disk_read_bps, _last_disk_write_bps, _last_disk_speeds, _last_disk_active, _last_disk_details = native_sample
        _last_disk_speed_time = current_time
        _last_disk_io_time = current_time
        return native_sample

    current = psutil.disk_io_counters(perdisk=True) or {}
    elapsed = max(current_time - _last_disk_io_time, 0.001)
    elapsed_ms = elapsed * 1000
    speeds: dict[str, tuple[int, int]] = {}
    active: dict[str, float] = {}
    details: dict[str, dict[str, Any]] = {}
    total_read = 0
    total_write = 0
    has_sample = False

    for name, after in current.items():
        before = _last_disk_io.get(name)
        if before is None:
            continue
        read_bps = int(max(0, (after.read_bytes - before.read_bytes) / elapsed))
        write_bps = int(max(0, (after.write_bytes - before.write_bytes) / elapsed))
        key = _disk_io_key(name)
        read_ms = max(0, getattr(after, "read_time", 0) - getattr(before, "read_time", 0))
        write_ms = max(0, getattr(after, "write_time", 0) - getattr(before, "write_time", 0))
        speeds[key] = (read_bps, write_bps)
        active[key] = round(min(((read_ms + write_ms) / elapsed_ms) * 100, 100.0), 1)
        details[key] = {
            "activeTimePercent": active[key],
            "averageResponseTimeMs": None,
            "queueLength": None,
        }
        total_read += read_bps
        total_write += write_bps
        has_sample = True

    _last_disk_io = current
    _last_disk_io_time = current_time
    _last_disk_speed_time = current_time
    _last_disk_speeds = speeds
    _last_disk_active = active
    _last_disk_details = details
    _last_disk_read_bps = total_read if has_sample else None
    _last_disk_write_bps = total_write if has_sample else None
    return _last_disk_read_bps, _last_disk_write_bps, _last_disk_speeds, _last_disk_active, _last_disk_details


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


def _disk_active_for(row: dict[str, Any], active_times: dict[str, float]) -> float | None:
    if hardware_info is None:
        return None

    candidates = []
    disk_index = hardware_info._to_int(row.get("DiskIndex"))
    if disk_index is not None:
        candidates.append(f"physicaldrive{disk_index}")
    device_id = hardware_info._clean_text(row.get("DiskDeviceId"))
    if device_id:
        candidates.append(device_id.replace("\\\\.\\", "").lower())
    logical = hardware_info._clean_text(row.get("DeviceId"))
    if logical:
        candidates.append(logical.lower())

    for candidate in candidates:
        value = active_times.get(candidate)
        if value is not None:
            return value
    return None


def _disk_details_for(row: dict[str, Any], disk_details: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if hardware_info is None:
        return {}

    candidates = []
    disk_index = hardware_info._to_int(row.get("DiskIndex"))
    if disk_index is not None:
        candidates.append(f"physicaldrive{disk_index}")
    device_id = hardware_info._clean_text(row.get("DiskDeviceId"))
    if device_id:
        candidates.append(device_id.replace("\\\\.\\", "").lower())
    logical = hardware_info._clean_text(row.get("DeviceId"))
    if logical:
        candidates.append(logical.lower())

    for candidate in candidates:
        value = disk_details.get(candidate)
        if value:
            return value
    return {}


def _get_disk_devices(
    io_speeds: dict[str, tuple[int, int]] | None = None,
    active_times: dict[str, float] | None = None,
    disk_details: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    io_speeds = io_speeds or {}
    active_times = active_times or {}
    disk_details = disk_details or {}
    if hardware_info is None:
        try:
            usage = psutil.disk_usage(_get_system_disk_path())
            read_bps, write_bps = next(iter(io_speeds.values()), (None, None))
            active_percent = next(iter(active_times.values()), None)
            return [{
                "mountpoint": _get_system_disk_path(),
                "partitions": _get_system_disk_path(),
                "device": _get_system_disk_path(),
                "totalBytes": usage.total,
                "usedBytes": usage.used,
                "freeBytes": usage.free,
                "usagePercent": active_percent,
                "activeTimePercent": active_percent,
                "capacityUsagePercent": round(usage.percent, 1),
                "readBytesPerSecond": read_bps,
                "writeBytesPerSecond": write_bps,
                "averageResponseTimeMs": next((details.get("averageResponseTimeMs") for details in disk_details.values() if details), None),
                "queueLength": next((details.get("queueLength") for details in disk_details.values() if details), None),
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
        read_bps, write_bps = hardware_info._disk_speed_for(representative, io_speeds)
        active_percent = _disk_active_for(representative, active_times)
        detail = _disk_details_for(representative, disk_details)
        capacity_percent = round((used / total) * 100, 1)
        disks.append({
            "mountpoint": ", ".join(hardware_info._unique_text(mountpoints)),
            "partitions": ", ".join(hardware_info._unique_text(mountpoints)),
            "device": physical_device or ", ".join(hardware_info._unique_text([row.get("DeviceId") for row in rows])),
            "totalBytes": total,
            "usedBytes": used,
            "freeBytes": free,
            "usagePercent": active_percent,
            "activeTimePercent": active_percent,
            "capacityUsagePercent": capacity_percent,
            "readBytesPerSecond": read_bps,
            "writeBytesPerSecond": write_bps,
            "averageResponseTimeMs": detail.get("averageResponseTimeMs"),
            "queueLength": detail.get("queueLength"),
        })

    return disks


def _disk_active_percent(disks: list[dict[str, Any]]) -> float | None:
    values = [
        _to_float(disk.get("activeTimePercent") if disk.get("activeTimePercent") is not None else disk.get("usagePercent"))
        for disk in disks
    ]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _sum_disk_speed(disks: list[dict[str, Any]], key: str, fallback: int | None) -> int | None:
    values = [_to_int(disk.get(key)) for disk in disks]
    values = [value for value in values if value is not None]
    return sum(values) if values else fallback


def collect_metrics() -> list[dict[str, Any]]:
    """Collect Windows metrics using the same payload shape as Linux."""
    global _last_net_sent, _last_net_recv, _last_net_interfaces, _last_time

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu_perf = _get_cpu_perf()
    cpu_logical = [round(value, 1) for value in psutil.cpu_percent(interval=None, percpu=True)]
    cpu_percent = _first_available(
        cpu_perf.get("utilityPercent"),
        cpu_perf.get("usagePercent"),
        round(sum(cpu_logical) / len(cpu_logical), 1) if cpu_logical else None,
    )
    mem_percent = round(mem.percent, 1)

    current_time = time.time()
    time_diff = max(current_time - _last_time, 1)

    cur_sent, cur_recv = _get_net_io()
    cur_net_interfaces = _get_net_io_per_interface()
    sent_bps = int(max(0, (cur_sent - _last_net_sent) / time_diff))
    recv_bps = int(max(0, (cur_recv - _last_net_recv) / time_diff))
    network_interfaces = _network_interface_speeds(cur_net_interfaces, _last_net_interfaces, time_diff)
    _last_net_sent, _last_net_recv = cur_sent, cur_recv
    _last_net_interfaces = cur_net_interfaces
    _last_time = current_time

    freq = psutil.cpu_freq()
    cpu_freq_mhz = _first_available(
        cpu_perf.get("currentSpeedMhz"),
        round(freq.current, 1) if freq else None,
    )

    disk_read_bps, disk_write_bps, disk_speeds, disk_active, disk_details = _get_disk_io_speeds(current_time)
    disk_devices = _get_disk_devices(disk_speeds, disk_active, disk_details)
    disk_percent = _disk_active_percent(disk_devices)
    disk_read_bps = _sum_disk_speed(disk_devices, "readBytesPerSecond", disk_read_bps)
    disk_write_bps = _sum_disk_speed(disk_devices, "writeBytesPerSecond", disk_write_bps)

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
        _metric(15, disk_devices),
        _metric(16, network_interfaces),
        _metric(17, cpu_logical),
    ]


def get_self_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return ""


_last_net_sent, _last_net_recv = _get_net_io()
_last_net_interfaces = _get_net_io_per_interface()
_last_time = time.time()

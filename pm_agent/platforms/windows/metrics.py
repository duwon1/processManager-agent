"""Windows system metric collection."""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Any

import psutil

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
}

_last_net_sent: int
_last_net_recv: int
_last_time: float
_last_disk_io = psutil.disk_io_counters()


def _metric(metric_id: int, value: Any) -> dict[str, Any]:
    key, unit = METRIC_DEFINITIONS[metric_id]
    return {
        "id": metric_id,
        "key": key,
        "title": key,
        "value": value,
        "rawValue": value,
        "unit": unit,
        "valueType": "number" if isinstance(value, (int, float)) else "object" if isinstance(value, dict) else "text",
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


def _get_gpu_usage() -> float | None:
    try:
        import GPUtil

        gpus = GPUtil.getGPUs()
        if gpus:
            return round(gpus[0].load * 100, 1)
    except Exception:
        pass
    return None


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

    cached_bytes = getattr(mem, "cached", 0) or 0
    committed_bytes = mem.used + getattr(swap, "used", 0)

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
        _metric(14, None),
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

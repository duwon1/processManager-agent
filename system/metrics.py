"""시스템 메트릭(CPU·GPU·메모리·디스크·네트워크·하드웨어)을 표준 단위로 수집합니다."""
import glob
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
from fastapi import APIRouter

router = APIRouter()

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

# ── GPU 사용률 ──────────────────────────────────────────────────────────────

_last_rc6_ms: float | None = None
_last_rc6_time: float | None = None


def get_gpu_usage() -> float | None:
    """GPU 사용률을 percent 숫자로 반환합니다. GPUtil → sysfs → Intel RC6 순으로 시도합니다."""
    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
        if gpus:
            return round(gpus[0].load * 100, 1)
    except Exception:
        pass

    for i in range(10):
        path = Path(f"/sys/class/drm/card{i}/device/gpu_busy_percent")
        try:
            return round(float(path.read_text(encoding="utf-8").strip()), 1)
        except Exception:
            pass

    return _get_intel_gpu_usage()


def _get_intel_gpu_usage() -> float | None:
    """Intel GPU 사용률을 RC6 residency 또는 주파수 비율로 추정해 percent 숫자로 반환합니다."""
    global _last_rc6_ms, _last_rc6_time

    rc6_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rc6_residency_ms")
    if not rc6_paths:
        act_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rps_act_freq_mhz")
        max_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rps_max_freq_mhz")
        try:
            act = int(Path(act_paths[0]).read_text(encoding="utf-8").strip())
            mx = int(Path(max_paths[0]).read_text(encoding="utf-8").strip())
            if mx > 0:
                return round(act / mx * 100, 1)
        except Exception:
            pass
        return None

    try:
        rc6_ms = int(Path(rc6_paths[0]).read_text(encoding="utf-8").strip())
        now = time.time()
        if _last_rc6_ms is None:
            _last_rc6_ms, _last_rc6_time = rc6_ms, now
            return None
        dt_ms = (now - _last_rc6_time) * 1000
        drc6 = rc6_ms - _last_rc6_ms
        _last_rc6_ms, _last_rc6_time = rc6_ms, now
        if dt_ms <= 0:
            return None
        return round(max(0.0, (1 - drc6 / dt_ms) * 100), 1)
    except Exception:
        return None


# ── 네트워크 속도 ────────────────────────────────────────────────────────────

def _get_net_io() -> tuple[int, int]:
    """루프백을 제외한 전체 네트워크 I/O 바이트 누적값을 반환합니다."""
    counters = psutil.net_io_counters(pernic=True)
    sent, recv = 0, 0
    for nic, counter in counters.items():
        if nic == "lo":
            continue
        sent += counter.bytes_sent
        recv += counter.bytes_recv
    return sent, recv


_last_net_sent, _last_net_recv = _get_net_io()
_last_time = time.time()

# 디스크 I/O 델타 추적 (네트워크와 동일한 방식)
_last_disk_io = psutil.disk_io_counters()


# ── 메모리 하드웨어 정보 ────────────────────────────────────────────────────

_memory_hardware_cache: dict[str, Any] | None = None
_memory_hardware_loaded = False


def _get_memory_hardware() -> dict[str, Any] | None:
    """dmidecode 결과를 슬롯 수·총량 bytes·속도 MT/s 같은 표준 숫자 필드로 반환합니다."""
    global _memory_hardware_cache, _memory_hardware_loaded
    if _memory_hardware_loaded:
        return _memory_hardware_cache

    try:
        output = subprocess.check_output(
            ["sudo", "dmidecode", "-t", "17"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        size_lines = re.findall(r"Size:\s+(\d+)\s+(MB|GB)", output)
        speeds = re.findall(r"Speed:\s+(\d+)\s+MT/s", output)
        mem_types = re.findall(r"\n\s+Type:\s+([^\n]+)", output)

        total_bytes = sum(
            int(value) * (1024 ** 3 if unit == "GB" else 1024 ** 2)
            for value, unit in size_lines
        )
        if total_bytes == 0:
            _memory_hardware_cache = None
            _memory_hardware_loaded = True
            return None

        slot_count = len(size_lines)
        _memory_hardware_cache = {
            "slotsUsed": slot_count,
            "totalBytes": total_bytes,
            "perSlotBytes": int(total_bytes / slot_count) if slot_count else None,
            "memoryType": mem_types[0].strip() if mem_types else None,
            "speedMtPerSecond": int(speeds[0]) if speeds else None,
        }
        _memory_hardware_loaded = True
        return _memory_hardware_cache
    except Exception:
        _memory_hardware_cache = None
        _memory_hardware_loaded = True
        return None


def _metric(metric_id: int, value: Any) -> dict[str, Any]:
    """메트릭을 key/rawValue/unit 기반 공통 구조로 생성합니다."""
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


# ── 메트릭 수집 ──────────────────────────────────────────────────────────────

def collect_system_metrics() -> list[dict[str, Any]]:
    """현재 시스템 메트릭을 화면 문자열 없이 표준 숫자 단위로 수집합니다."""
    global _last_time, _last_net_sent, _last_net_recv, _last_disk_io

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu_percent = round(psutil.cpu_percent(interval=None), 1)
    mem_percent = round(mem.percent, 1)
    disk_percent = round(psutil.disk_usage("/").percent, 1)
    gpu_usage = get_gpu_usage()

    cur_sent, cur_recv = _get_net_io()
    current_time = time.time()
    time_diff = max(current_time - _last_time, 1)
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

    return [
        _metric(1, cpu_percent),
        _metric(2, gpu_usage),
        _metric(3, mem_percent),
        _metric(4, disk_percent),
        _metric(5, sent_bps),
        _metric(6, recv_bps),
        _metric(7, cpu_freq_mhz),
        _metric(8, mem.used),
        _metric(9, mem.available),
        _metric(10, getattr(mem, "cached", 0) or 0),
        _metric(11, mem.used + swap.used),
        _metric(12, disk_read_bps),
        _metric(13, disk_write_bps),
        _metric(14, _get_memory_hardware()),
    ]


def get_self_ip() -> str:
    """외부로 나가는 실제 IP 주소를 반환합니다. 조회 실패 시 빈 문자열을 반환합니다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return ""

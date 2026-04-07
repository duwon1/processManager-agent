"""시스템 메트릭(CPU·GPU·메모리·디스크·네트워크·하드웨어)을 수집합니다."""
import glob
import re
import socket
import subprocess
import time
from pathlib import Path

import psutil
from fastapi import APIRouter

router = APIRouter()

# ── GPU 사용률 ──────────────────────────────────────────────────────────────

_last_rc6_ms: float | None = None
_last_rc6_time: float | None = None


def get_gpu_usage() -> str:
    """GPU 사용률을 문자열로 반환합니다. GPUtil → sysfs → Intel RC6 순으로 시도합니다."""
    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
        if gpus:
            return f"{gpus[0].load * 100:.1f}%"
    except Exception:
        pass

    for i in range(10):
        path = Path(f"/sys/class/drm/card{i}/device/gpu_busy_percent")
        try:
            return f"{path.read_text(encoding='utf-8').strip()}%"
        except Exception:
            pass

    return _get_intel_gpu_usage()


def _get_intel_gpu_usage() -> str:
    """Intel GPU 사용률을 RC6 residency 또는 주파수 비율로 추정합니다."""
    global _last_rc6_ms, _last_rc6_time

    rc6_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rc6_residency_ms")
    if not rc6_paths:
        act_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rps_act_freq_mhz")
        max_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rps_max_freq_mhz")
        try:
            act = int(Path(act_paths[0]).read_text(encoding='utf-8').strip())
            mx = int(Path(max_paths[0]).read_text(encoding='utf-8').strip())
            if mx > 0:
                return f"{act / mx * 100:.1f}%"
        except Exception:
            pass
        return "N/A"

    try:
        rc6_ms = int(Path(rc6_paths[0]).read_text(encoding='utf-8').strip())
        now = time.time()
        if _last_rc6_ms is None:
            _last_rc6_ms, _last_rc6_time = rc6_ms, now
            return "N/A"
        dt_ms = (now - _last_rc6_time) * 1000
        drc6 = rc6_ms - _last_rc6_ms
        _last_rc6_ms, _last_rc6_time = rc6_ms, now
        if dt_ms <= 0:
            return "N/A"
        usage = max(0.0, (1 - drc6 / dt_ms) * 100)
        return f"{usage:.1f}%"
    except Exception:
        return "N/A"


# ── 네트워크 속도 ────────────────────────────────────────────────────────────

def _get_net_io() -> tuple[int, int]:
    """루프백을 제외한 전체 네트워크 I/O 바이트를 반환합니다."""
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


# ── 메모리 하드웨어 정보 ────────────────────────────────────────────────────

_memory_hardware_cache: str | None = None


def _get_memory_hardware() -> str:
    """dmidecode로 메모리 슬롯·타입·속도 정보를 읽어 한 줄 문자열로 반환합니다.
    결과는 프로세스 생애 동안 캐시됩니다 (하드웨어는 런타임에 변하지 않으므로).
    """
    global _memory_hardware_cache
    if _memory_hardware_cache is not None:
        return _memory_hardware_cache

    try:
        output = subprocess.check_output(
            ["sudo", "dmidecode", "-t", "17"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        # 실제 장착된 슬롯만 추출 (No Module Installed 제외)
        size_lines = re.findall(r'Size:\s+(\d+)\s+(MB|GB)', output)
        speeds = re.findall(r'Speed:\s+(\d+)\s+MT/s', output)
        mem_types = re.findall(r'\n\s+Type:\s+([^\n]+)', output)

        total_mb = sum(
            int(v) * (1024 if unit == "GB" else 1)
            for v, unit in size_lines
        )

        if total_mb == 0:
            _memory_hardware_cache = "N/A"
            return _memory_hardware_cache

        slot_count = len(size_lines)
        total_gb = total_mb / 1024
        mem_type = mem_types[0].strip() if mem_types else "Unknown"
        speed = speeds[0] if speeds else "?"
        per_slot_gb = int(total_gb / slot_count) if slot_count else int(total_gb)

        _memory_hardware_cache = (
            f"{slot_count}슬롯 × {per_slot_gb}GB {mem_type} @ {speed}MT/s "
            f"(총 {total_gb:.0f}GB)"
        )
    except Exception:
        _memory_hardware_cache = "N/A"

    return _memory_hardware_cache


# ── 메트릭 수집 ──────────────────────────────────────────────────────────────

def collect_system_metrics() -> list:
    """현재 시스템 메트릭을 수집해 대시보드 표시 형식으로 반환합니다."""
    global _last_time, _last_net_sent, _last_net_recv

    cpu_percent = psutil.cpu_percent(interval=None)
    mem_percent = psutil.virtual_memory().percent
    disk_percent = psutil.disk_usage("/").percent
    gpu_usage = get_gpu_usage()

    cur_sent, cur_recv = _get_net_io()
    current_time = time.time()
    time_diff = max(current_time - _last_time, 1)
    sent_bps = (cur_sent - _last_net_sent) / time_diff
    recv_bps = (cur_recv - _last_net_recv) / time_diff
    _last_net_sent, _last_net_recv, _last_time = cur_sent, cur_recv, current_time

    def fmt_speed(bps: float) -> str:
        if bps >= 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} MB/s"
        return f"{bps / 1024:.1f} KB/s"

    return [
        {"id": 1,  "title": "CPU 사용률",    "value": f"{cpu_percent}%"},
        {"id": 2,  "title": "GPU 사용률",    "value": gpu_usage},
        {"id": 3,  "title": "메모리 사용률", "value": f"{mem_percent}%"},
        {"id": 4,  "title": "디스크 사용률", "value": f"{disk_percent}%"},
        {"id": 5,  "title": "업로드 속도",   "value": fmt_speed(sent_bps)},
        {"id": 6,  "title": "다운로드 속도", "value": fmt_speed(recv_bps)},
        {"id": 14, "title": "메모리 구성",   "value": _get_memory_hardware()},
    ]


def get_self_ip() -> str:
    """외부로 나가는 실제 IP 주소를 반환합니다. 조회 실패 시 빈 문자열을 반환합니다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return ""

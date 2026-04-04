import glob
import socket
import time
from pathlib import Path

import psutil
from fastapi import APIRouter

router = APIRouter()

_last_rc6_ms: float | None   = None
_last_rc6_time: float | None = None


def get_gpu_usage() -> str:
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
    global _last_rc6_ms, _last_rc6_time

    rc6_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rc6_residency_ms")
    if not rc6_paths:
        act_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rps_act_freq_mhz")
        max_paths = glob.glob("/sys/class/drm/card*/gt/gt*/rps_max_freq_mhz")
        try:
            act = int(Path(act_paths[0]).read_text(encoding='utf-8').strip())
            mx  = int(Path(max_paths[0]).read_text(encoding='utf-8').strip())
            if mx > 0:
                return f"{act / mx * 100:.1f}%"
        except Exception:
            pass
        return "N/A"

    try:
        rc6_ms = int(Path(rc6_paths[0]).read_text(encoding='utf-8').strip())
        now    = time.time()
        if _last_rc6_ms is None:
            _last_rc6_ms, _last_rc6_time = rc6_ms, now
            return "N/A"
        dt_ms  = (now - _last_rc6_time) * 1000
        drc6   = rc6_ms - _last_rc6_ms
        _last_rc6_ms, _last_rc6_time = rc6_ms, now
        if dt_ms <= 0:
            return "N/A"
        usage = max(0.0, (1 - drc6 / dt_ms) * 100)
        return f"{usage:.1f}%"
    except Exception:
        return "N/A"


def _get_net_io() -> tuple[int, int]:
    counters = psutil.net_io_counters(pernic=True)
    sent, recv = 0, 0
    for nic, counter in counters.items():
        if nic == "lo":
            continue
        sent += counter.bytes_sent
        recv += counter.bytes_recv
    return sent, recv


last_net_sent, last_net_recv = _get_net_io()
last_time = time.time()


def collect_system_metrics() -> list:
    """현재 시스템 메트릭을 수집해 대시보드 표시 형식으로 반환합니다."""
    global last_time, last_net_sent, last_net_recv

    cpu_percent  = psutil.cpu_percent(interval=None)
    mem_percent  = psutil.virtual_memory().percent
    disk_percent = psutil.disk_usage("/").percent
    gpu_usage    = get_gpu_usage()

    cur_sent, cur_recv = _get_net_io()
    current_time = time.time()
    time_diff    = max(current_time - last_time, 1)
    sent_bps     = (cur_sent - last_net_sent) / time_diff
    recv_bps     = (cur_recv - last_net_recv) / time_diff
    last_net_sent, last_net_recv, last_time = cur_sent, cur_recv, current_time

    def fmt_speed(bps: float) -> str:
        if bps >= 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} MB/s"
        return f"{bps / 1024:.1f} KB/s"

    return [
        {"id": 1, "title": "CPU 사용률",    "value": f"{cpu_percent}%"},
        {"id": 2, "title": "GPU 사용률",    "value": f"{gpu_usage}"},
        {"id": 3, "title": "메모리 사용률", "value": f"{mem_percent}%"},
        {"id": 4, "title": "디스크 사용률", "value": f"{disk_percent}%"},
        {"id": 5, "title": "업로드 속도",   "value": fmt_speed(sent_bps)},
        {"id": 6, "title": "다운로드 속도", "value": fmt_speed(recv_bps)},
    ]


def get_self_ip() -> str:
    """외부로 나가는 실제 IP 주소를 반환합니다. 조회 실패 시 빈 문자열을 반환합니다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return ""

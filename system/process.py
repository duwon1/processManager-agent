import os
import signal
import time
from datetime import datetime
from typing import Dict, List

import psutil
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/process", tags=["Process"])

CPU_COUNT = psutil.cpu_count() or 1
PROCESS_LIMIT = None
CPU_SAMPLE_INTERVAL = 0.1
MAX_CMDLINE_LENGTH = 160
MAX_EXE_LENGTH = 120

# 이전 I/O 측정값 캐시: pid -> (prev_read_bytes, prev_write_bytes, timestamp)
_io_cache: dict[int, tuple[float, float, float]] = {}


def normalize_status(status: str | None) -> str:
    status_map = {
        "running": "running", "sleeping": "sleeping", "disk-sleep": "disk_sleep",
        "stopped": "stopped", "tracing-stop": "tracing_stop", "zombie": "zombie",
        "dead": "dead", "wake-kill": "wake_kill", "waking": "waking",
        "parked": "parked", "idle": "idle", "locked": "locked",
        "waiting": "waiting", "suspended": "suspended",
    }
    return status_map.get((status or "").lower(), (status or "unknown").lower())


def truncate_text(value: str | None, max_length: int) -> str:
    if not value:
        return ""
    normalized = value.strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3] + "..."


def format_cmdline(cmdline: List[str] | None) -> str:
    if not cmdline:
        return ""
    joined = " ".join(part for part in cmdline if part).strip()
    return truncate_text(joined, MAX_CMDLINE_LENGTH)


def format_exe_path(exe_path: str | None) -> str:
    return truncate_text(exe_path, MAX_EXE_LENGTH)


def format_started_at(create_time: float | None) -> str | None:
    if not create_time:
        return None
    try:
        return datetime.fromtimestamp(create_time).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def get_io_speed(proc: psutil.Process) -> tuple[int, int]:
    """프로세스의 디스크 읽기/쓰기 속도를 bytes/sec 단위로 반환합니다.
    이전 측정값과의 차이를 경과 시간으로 나눠 초당 속도를 계산합니다.
    첫 측정 또는 권한 부족 시 (0, 0)을 반환합니다.
    """
    global _io_cache
    pid = proc.pid
    try:
        io = proc.io_counters()
        now = time.time()
        if pid in _io_cache:
            prev_read, prev_write, prev_time = _io_cache[pid]
            elapsed = now - prev_time
            if elapsed > 0:
                read_speed = int(max(0, (io.read_bytes - prev_read) / elapsed))
                write_speed = int(max(0, (io.write_bytes - prev_write) / elapsed))
            else:
                read_speed, write_speed = 0, 0
        else:
            read_speed, write_speed = 0, 0
        _io_cache[pid] = (io.read_bytes, io.write_bytes, now)
        return read_speed, write_speed
    except (psutil.AccessDenied, AttributeError, psutil.NoSuchProcess):
        _io_cache.pop(pid, None)
        return 0, 0


def prime_cpu_percent(processes: List[psutil.Process]) -> None:
    for proc in processes:
        try:
            proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(CPU_SAMPLE_INTERVAL)


def get_process_data() -> List[Dict]:
    attrs = [
        "pid", "name", "username", "status",
        "memory_info", "memory_percent",
        "create_time", "cmdline", "exe", "num_threads",
    ]
    processes = []
    for proc in psutil.process_iter(attrs):
        try:
            if proc.info.get("pid") == 0:
                continue
            processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    prime_cpu_percent(processes)

    # 종료된 PID 캐시 정리 (메모리 누수 방지)
    alive_pids = {proc.pid for proc in processes}
    for dead_pid in list(_io_cache.keys()):
        if dead_pid not in alive_pids:
            del _io_cache[dead_pid]

    process_list = []
    for proc in processes:
        try:
            pinfo = proc.info
            cpu_percent = round(proc.cpu_percent(interval=None) / CPU_COUNT, 1)
            mem_info = pinfo.get("memory_info")
            memory_bytes = mem_info.rss if mem_info else 0
            memory_percent = round(pinfo.get("memory_percent") or 0.0, 1)
            disk_read_bps, disk_write_bps = get_io_speed(proc)
            process_list.append({
                "pid": pinfo.get("pid"),
                "name": pinfo.get("name") or "Unknown",
                "username": pinfo.get("username") or "-",
                "status": normalize_status(pinfo.get("status")),
                "cpu_percent": max(cpu_percent, 0.0),
                "memory_bytes": memory_bytes,
                "memory_percent": memory_percent,
                "disk_read_bytes_per_second": disk_read_bps,
                "disk_write_bytes_per_second": disk_write_bps,
                "thread_count": pinfo.get("num_threads") or 0,
                "started_at": format_started_at(pinfo.get("create_time")),
                "cmdline": format_cmdline(pinfo.get("cmdline")),
                "exe": format_exe_path(pinfo.get("exe")),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return sorted(
        process_list,
        key=lambda item: (item["cpu_percent"], item["memory_bytes"]),
        reverse=True,
    )[:PROCESS_LIMIT]


def kill_process_by_pid(pid: int) -> str:
    try:
        os.kill(pid, signal.SIGKILL)
        return f"PID {pid} 종료했습니다."
    except ProcessLookupError as exc:
        raise HTTPException(status_code=404, detail=f"프로세스 {pid}를 찾을 수 없습니다.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"프로세스 {pid} 종료 권한이 없습니다.") from exc


@router.get("/all", response_model=List[Dict])
def get_all_processes_http():
    return get_process_data()


@router.delete("/{pid}")
def kill_process(pid: int):
    return {"message": kill_process_by_pid(pid)}

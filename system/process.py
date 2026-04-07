import os
import signal
import time
from datetime import datetime
from typing import Dict, List

import psutil
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/process", tags=["Process"])

CPU_COUNT = psutil.cpu_count() or 1
PROCESS_LIMIT = None  # 한 번에 전송할 최대 프로세스 수 (None = 전체)
CPU_SAMPLE_INTERVAL = 0.1  # CPU 사용률 측정을 위한 샘플링 대기 시간(초)
MAX_CMDLINE_LENGTH = 160  # 명령행 문자열 최대 길이
MAX_EXE_LENGTH = 120  # 실행 파일 경로 최대 길이


def normalize_status(status: str | None) -> str:
    """psutil 상태 문자열을 대시보드 표시용 표준 값으로 변환합니다."""
    status_map = {
        "running": "running",
        "sleeping": "sleeping",
        "disk-sleep": "disk_sleep",
        "stopped": "stopped",
        "tracing-stop": "tracing_stop",
        "zombie": "zombie",
        "dead": "dead",
        "wake-kill": "wake_kill",
        "waking": "waking",
        "parked": "parked",
        "idle": "idle",
        "locked": "locked",
        "waiting": "waiting",
        "suspended": "suspended",
    }
    return status_map.get((status or "").lower(), (status or "unknown").lower())


def truncate_text(value: str | None, max_length: int) -> str:
    """문자열이 max_length를 초과하면 말줄임표(...)를 붙여 잘라 반환합니다."""
    if not value:
        return ""
    normalized = value.strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3] + "..."


def format_cmdline(cmdline: List[str] | None) -> str:
    """프로세스 argv 목록을 공백으로 합친 뒤 최대 길이로 잘라 반환합니다."""
    if not cmdline:
        return ""
    joined = " ".join(part for part in cmdline if part).strip()
    return truncate_text(joined, MAX_CMDLINE_LENGTH)


def format_exe_path(exe_path: str | None) -> str:
    """실행 파일 경로를 최대 길이로 잘라 반환합니다."""
    return truncate_text(exe_path, MAX_EXE_LENGTH)


def format_started_at(create_time: float | None) -> str | None:
    """프로세스 생성 타임스탬프를 ISO 8601 형식 문자열로 변환합니다."""
    if not create_time:
        return None
    try:
        return datetime.fromtimestamp(create_time).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def get_io_totals(proc: psutil.Process) -> tuple[float, float]:
    """프로세스의 누적 디스크 읽기·쓰기량을 MB 단위로 반환합니다.
    권한 부족 등으로 조회 불가능한 경우 (0.0, 0.0)을 반환합니다.
    """
    try:
        io = proc.io_counters()
        read_mb = round(io.read_bytes / (1024 * 1024), 2)
        write_mb = round(io.write_bytes / (1024 * 1024), 2)
        return read_mb, write_mb
    except (psutil.AccessDenied, AttributeError, psutil.NoSuchProcess):
        return 0.0, 0.0


def prime_cpu_percent(processes: List[psutil.Process]) -> None:
    """CPU 사용률 측정 전 기준값을 초기화합니다.
    psutil은 두 번 호출 간의 차이로 CPU %를 계산하므로 첫 호출은 항상 0을 반환합니다.
    샘플링 대기 후 실제 측정값을 얻을 수 있습니다.
    """
    for proc in processes:
        try:
            proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(CPU_SAMPLE_INTERVAL)


def get_process_data() -> List[Dict]:
    """실행 중인 프로세스 목록을 수집해 CPU 사용률 내림차순으로 정렬한 뒤 반환합니다.
    PID 0(커널 유휴 프로세스)과 접근 불가 프로세스는 제외합니다.
    """
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

    process_list = []
    for proc in processes:
        try:
            pinfo = proc.info
            cpu_percent = round(proc.cpu_percent(interval=None) / CPU_COUNT, 1)
            mem_info = pinfo.get("memory_info")
            memory_mb = round((mem_info.rss if mem_info else 0) / (1024 * 1024), 2)
            memory_percent = round(pinfo.get("memory_percent") or 0.0, 1)
            disk_read_mb, disk_write_mb = get_io_totals(proc)

            process_list.append({
                "pid": pinfo.get("pid"),
                "name": pinfo.get("name") or "Unknown",
                "username": pinfo.get("username") or "-",
                "status": normalize_status(pinfo.get("status")),
                "cpu_percent": max(cpu_percent, 0.0),
                "memory_mb": memory_mb,
                "memory_percent": memory_percent,
                "disk_read_mb": disk_read_mb,
                "disk_write_mb": disk_write_mb,
                "thread_count": pinfo.get("num_threads") or 0,
                "started_at": format_started_at(pinfo.get("create_time")),
                "cmdline": format_cmdline(pinfo.get("cmdline")),
                "exe": format_exe_path(pinfo.get("exe")),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return sorted(
        process_list,
        key=lambda item: (item["cpu_percent"], item["memory_mb"]),
        reverse=True,
    )[:PROCESS_LIMIT]


def kill_process_by_pid(pid: int) -> str:
    """SIGKILL로 프로세스를 즉시 강제 종료합니다."""
    try:
        os.kill(pid, signal.SIGKILL)
        return f"PID {pid} 종료했습니다."
    except ProcessLookupError as exc:
        raise HTTPException(status_code=404, detail=f"프로세스 {pid}를 찾을 수 없습니다.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"프로세스 {pid} 종료 권한이 없습니다.") from exc


@router.get("/all", response_model=List[Dict])
def get_all_processes_http():
    """현재 프로세스 목록을 HTTP로 즉시 조회합니다. 디버깅용으로 사용합니다."""
    return get_process_data()


@router.delete("/{pid}")
def kill_process(pid: int):
    """PID로 프로세스를 종료합니다. WebSocket 흐름과 동일한 kill 함수를 사용합니다."""
    return {"message": kill_process_by_pid(pid)}

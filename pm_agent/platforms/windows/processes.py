"""Windows process collection and control."""
from __future__ import annotations

from datetime import datetime
import os
import time
from typing import Any

import psutil

try:
    from pm_agent.platforms.windows import native_process_snapshot
except Exception:
    native_process_snapshot = None


CPU_COUNT = psutil.cpu_count() or 1
CPU_SAMPLE_INTERVAL = 0.1
MAX_CMDLINE_LENGTH = 160
MAX_EXE_LENGTH = 120

_io_cache: dict[int, tuple[float, float, float]] = {}
_native_fallback_reported = False


def _normalize_status(status: str | None) -> str:
    status_map = {
        "running": "running",
        "sleeping": "sleeping",
        "stopped": "stopped",
        "zombie": "zombie",
        "dead": "dead",
        "idle": "idle",
        "locked": "locked",
        "waiting": "waiting",
        "suspended": "suspended",
    }
    return status_map.get((status or "").lower(), (status or "unknown").lower())


def _truncate(value: str | None, max_length: int) -> str:
    if not value:
        return ""
    normalized = value.strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3] + "..."


def _format_cmdline(cmdline: list[str] | None) -> str:
    if not cmdline:
        return ""
    return _truncate(" ".join(part for part in cmdline if part).strip(), MAX_CMDLINE_LENGTH)


def _format_started_at(create_time: float | None) -> str | None:
    if not create_time:
        return None
    try:
        return datetime.fromtimestamp(create_time).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _get_io_speed(proc: psutil.Process) -> tuple[int, int]:
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
    except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError, OSError):
        _io_cache.pop(pid, None)
        return 0, 0


def _prime_cpu_percent(processes: list[psutil.Process]) -> None:
    for proc in processes:
        try:
            proc.cpu_percent(interval=None)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    time.sleep(CPU_SAMPLE_INTERVAL)


def _list_processes_psutil() -> list[dict[str, Any]]:
    attrs = [
        "pid",
        "name",
        "username",
        "status",
        "memory_info",
        "memory_percent",
        "create_time",
        "cmdline",
        "exe",
        "num_threads",
    ]

    processes: list[psutil.Process] = []
    for proc in psutil.process_iter(attrs):
        try:
            if proc.info.get("pid") == 0:
                continue
            processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue

    _prime_cpu_percent(processes)

    alive_pids = {proc.pid for proc in processes}
    for dead_pid in list(_io_cache.keys()):
        if dead_pid not in alive_pids:
            del _io_cache[dead_pid]

    result: list[dict[str, Any]] = []
    for proc in processes:
        try:
            pinfo = proc.info
            mem_info = pinfo.get("memory_info")
            read_bps, write_bps = _get_io_speed(proc)
            result.append({
                "pid": pinfo.get("pid"),
                "name": pinfo.get("name") or "Unknown",
                "username": pinfo.get("username") or "-",
                "status": _normalize_status(pinfo.get("status")),
                "cpu_percent": max(round(proc.cpu_percent(interval=None) / CPU_COUNT, 1), 0.0),
                "memory_bytes": mem_info.rss if mem_info else 0,
                "memory_percent": round(pinfo.get("memory_percent") or 0.0, 1),
                "disk_read_bytes_per_second": read_bps,
                "disk_write_bytes_per_second": write_bps,
                "thread_count": pinfo.get("num_threads") or 0,
                "started_at": _format_started_at(pinfo.get("create_time")),
                "cmdline": _format_cmdline(pinfo.get("cmdline")),
                "exe": _truncate(pinfo.get("exe"), MAX_EXE_LENGTH),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue

    return sorted(
        result,
        key=lambda item: (item["cpu_percent"], item["memory_bytes"]),
        reverse=True,
    )


def list_processes() -> list[dict[str, Any]]:
    global _native_fallback_reported
    if native_process_snapshot is not None:
        try:
            return native_process_snapshot.list_processes()
        except Exception as exc:
            if not _native_fallback_reported:
                print(f"[에이전트] Windows 네이티브 프로세스 수집 실패, psutil로 전환: {exc}")
                _native_fallback_reported = True
    return _list_processes_psutil()


def kill_process(pid: int) -> str:
    if pid <= 0:
        raise RuntimeError("올바르지 않은 PID입니다.")
    if pid == os.getpid():
        raise RuntimeError("에이전트 자기 자신은 종료할 수 없습니다.")

    try:
        proc = psutil.Process(pid)
        name = proc.name()
        proc.kill()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            return f"PID {pid} 종료 명령을 보냈습니다."
        return f"PID {pid}({name}) 종료 완료"
    except psutil.NoSuchProcess as exc:
        raise RuntimeError(f"프로세스 {pid}를 찾을 수 없습니다.") from exc
    except psutil.AccessDenied as exc:
        raise RuntimeError(f"프로세스 {pid} 종료 권한이 없습니다.") from exc

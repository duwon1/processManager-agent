"""Fast Windows process snapshots backed by NtQuerySystemInformation."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime
import time
from typing import Any

import psutil


SYSTEM_PROCESS_INFORMATION_CLASS = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_BUFFER_OVERFLOW = 0x80000005
STATUS_BUFFER_TOO_SMALL = 0xC0000023

FILETIME_EPOCH_OFFSET = 116444736000000000
FILETIME_TICKS_PER_SECOND = 10_000_000
STATIC_CACHE_TTL_SECONDS = 600
MAX_CMDLINE_LENGTH = 160
MAX_EXE_LENGTH = 120

CPU_COUNT = psutil.cpu_count() or 1
TOTAL_MEMORY = psutil.virtual_memory().total or 1


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", wintypes.LONG),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG),
        ("SessionId", wintypes.ULONG),
        ("UniqueProcessKey", ctypes.c_size_t),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_longlong),
        ("WriteOperationCount", ctypes.c_longlong),
        ("OtherOperationCount", ctypes.c_longlong),
        ("ReadTransferCount", ctypes.c_longlong),
        ("WriteTransferCount", ctypes.c_longlong),
        ("OtherTransferCount", ctypes.c_longlong),
    ]


class CLIENT_ID(ctypes.Structure):
    _fields_ = [
        ("UniqueProcess", ctypes.c_void_p),
        ("UniqueThread", ctypes.c_void_p),
    ]


class SYSTEM_THREAD_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("KernelTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("CreateTime", ctypes.c_longlong),
        ("WaitTime", wintypes.ULONG),
        ("StartAddress", ctypes.c_void_p),
        ("ClientId", CLIENT_ID),
        ("Priority", wintypes.LONG),
        ("BasePriority", wintypes.LONG),
        ("ContextSwitches", wintypes.ULONG),
        ("ThreadState", wintypes.ULONG),
        ("WaitReason", wintypes.ULONG),
    ]


_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
_nt_query_system_information = _ntdll.NtQuerySystemInformation
_nt_query_system_information.argtypes = [
    wintypes.ULONG,
    ctypes.c_void_p,
    wintypes.ULONG,
    ctypes.POINTER(wintypes.ULONG),
]
_nt_query_system_information.restype = wintypes.LONG

_static_cache: dict[tuple[int, int], dict[str, Any]] = {}
_cpu_cache: dict[tuple[int, int], tuple[int, float]] = {}
_io_cache: dict[tuple[int, int], tuple[int, int, float]] = {}


def _truncate(value: str | None, max_length: int) -> str:
    if not value:
        return ""
    normalized = value.strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3] + "..."


def _filetime_to_timestamp(value: int) -> float | None:
    if value <= FILETIME_EPOCH_OFFSET:
        return None
    return (value - FILETIME_EPOCH_OFFSET) / FILETIME_TICKS_PER_SECOND


def _format_started_at(create_time: int) -> str | None:
    timestamp = _filetime_to_timestamp(create_time)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _read_unicode_string(value: UNICODE_STRING) -> str:
    if not value.Buffer or value.Length <= 0:
        return ""
    return ctypes.wstring_at(value.Buffer, value.Length // ctypes.sizeof(ctypes.c_wchar))


def _query_process_buffer() -> ctypes.Array[ctypes.c_char]:
    size = 1024 * 1024
    for _ in range(8):
        buffer = ctypes.create_string_buffer(size)
        return_length = wintypes.ULONG(0)
        status = _nt_query_system_information(
            SYSTEM_PROCESS_INFORMATION_CLASS,
            buffer,
            size,
            ctypes.byref(return_length),
        )
        unsigned_status = status & 0xFFFFFFFF
        if unsigned_status == 0:
            return buffer
        if unsigned_status in {
            STATUS_INFO_LENGTH_MISMATCH,
            STATUS_BUFFER_OVERFLOW,
            STATUS_BUFFER_TOO_SMALL,
        }:
            size = max(size * 2, int(return_length.value or 0) + 64 * 1024)
            continue
        raise OSError(f"NtQuerySystemInformation failed: 0x{unsigned_status:08x}")
    raise OSError("NtQuerySystemInformation buffer growth limit exceeded")


def _format_cmdline(cmdline: list[str] | None) -> str:
    if not cmdline:
        return ""
    return _truncate(" ".join(part for part in cmdline if part).strip(), MAX_CMDLINE_LENGTH)


def _get_static_info(pid: int, create_time: int, now: float) -> dict[str, str]:
    key = (pid, create_time)
    cached = _static_cache.get(key)
    if cached and now - float(cached["cached_at"]) < STATIC_CACHE_TTL_SECONDS:
        return cached

    username = "-"
    cmdline = ""
    exe = ""
    try:
        proc = psutil.Process(pid)
        try:
            username = proc.username() or "-"
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
        try:
            cmdline = _format_cmdline(proc.cmdline())
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
        try:
            exe = _truncate(proc.exe(), MAX_EXE_LENGTH)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass

    cached = {
        "cached_at": now,
        "username": username,
        "cmdline": cmdline,
        "exe": exe,
    }
    _static_cache[key] = cached
    return cached


def _cleanup_caches(alive_keys: set[tuple[int, int]]) -> None:
    for cache in (_static_cache, _cpu_cache, _io_cache):
        for key in list(cache.keys()):
            if key not in alive_keys:
                del cache[key]


def _cpu_percent(key: tuple[int, int], total_time: int, now: float) -> float:
    previous = _cpu_cache.get(key)
    _cpu_cache[key] = (total_time, now)
    if not previous:
        return 0.0

    previous_total, previous_time = previous
    elapsed = now - previous_time
    if elapsed <= 0:
        return 0.0

    delta_ticks = max(0, total_time - previous_total)
    percent = (delta_ticks / FILETIME_TICKS_PER_SECOND) / elapsed / CPU_COUNT * 100
    return max(round(percent, 1), 0.0)


def _io_speed(key: tuple[int, int], read_bytes: int, write_bytes: int, now: float) -> tuple[int, int]:
    previous = _io_cache.get(key)
    _io_cache[key] = (read_bytes, write_bytes, now)
    if not previous:
        return 0, 0

    previous_read, previous_write, previous_time = previous
    elapsed = now - previous_time
    if elapsed <= 0:
        return 0, 0

    read_speed = int(max(0, (read_bytes - previous_read) / elapsed))
    write_speed = int(max(0, (write_bytes - previous_write) / elapsed))
    return read_speed, write_speed


def _status_from_threads(entry_address: int, thread_count: int, cpu_percent: float) -> str:
    if cpu_percent > 0:
        return "running"
    if thread_count <= 0:
        return "unknown"

    try:
        thread_address = entry_address + ctypes.sizeof(SYSTEM_PROCESS_INFORMATION)
        states = []
        reasons = []
        thread_size = ctypes.sizeof(SYSTEM_THREAD_INFORMATION)
        for index in range(thread_count):
            thread = SYSTEM_THREAD_INFORMATION.from_address(thread_address + index * thread_size)
            states.append(int(thread.ThreadState))
            reasons.append(int(thread.WaitReason))
    except (ValueError, OSError):
        return "unknown"

    if any(state in {1, 2, 3} for state in states):
        return "running"
    if states and all(state == 5 for state in states) and any(reason in {5, 12} for reason in reasons):
        return "suspended"
    if states and all(state == 5 for state in states):
        return "sleeping"
    return "unknown"


def list_processes() -> list[dict[str, Any]]:
    buffer = _query_process_buffer()
    base_address = ctypes.addressof(buffer)
    offset = 0
    now = time.time()
    alive_keys: set[tuple[int, int]] = set()
    result: list[dict[str, Any]] = []

    while True:
        entry_address = base_address + offset
        entry = SYSTEM_PROCESS_INFORMATION.from_address(entry_address)
        pid = int(entry.UniqueProcessId or 0)
        if pid > 0:
            create_time = int(entry.CreateTime or 0)
            key = (pid, create_time)
            alive_keys.add(key)

            image_name = _read_unicode_string(entry.ImageName)
            if not image_name and pid == 4:
                image_name = "System"

            total_cpu_time = int(entry.KernelTime or 0) + int(entry.UserTime or 0)
            cpu_percent = _cpu_percent(key, total_cpu_time, now)
            read_bps, write_bps = _io_speed(
                key,
                int(entry.ReadTransferCount or 0),
                int(entry.WriteTransferCount or 0),
                now,
            )
            static_info = _get_static_info(pid, create_time, now)
            memory_bytes = int(entry.WorkingSetSize or 0)

            result.append({
                "pid": pid,
                "name": image_name or "Unknown",
                "username": static_info["username"],
                "status": _status_from_threads(entry_address, int(entry.NumberOfThreads or 0), cpu_percent),
                "cpu_percent": cpu_percent,
                "memory_bytes": memory_bytes,
                "memory_percent": round((memory_bytes / TOTAL_MEMORY) * 100, 1),
                "disk_read_bytes_per_second": read_bps,
                "disk_write_bytes_per_second": write_bps,
                "thread_count": int(entry.NumberOfThreads or 0),
                "handle_count": int(entry.HandleCount or 0),
                "base_priority": int(entry.BasePriority or 0),
                "session_id": int(entry.SessionId or 0),
                "started_at": _format_started_at(create_time),
                "cmdline": static_info["cmdline"],
                "exe": static_info["exe"],
            })

        if entry.NextEntryOffset == 0:
            break
        offset += int(entry.NextEntryOffset)

    _cleanup_caches(alive_keys)
    return sorted(
        result,
        key=lambda item: (item["cpu_percent"], item["memory_bytes"]),
        reverse=True,
    )

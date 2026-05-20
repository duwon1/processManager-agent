"""Physical disk sampling through the Windows PDH API."""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from threading import Lock
from typing import Any

from pm_agent.platforms.windows.native_gpu_usage import (
    ERROR_SUCCESS,
    PDH_FMT_COUNTERVALUE_ITEM_W,
    PDH_FMT_DOUBLE,
    PDH_INVALID_DATA,
    PDH_MORE_DATA,
    _pdh_add_english_counter,
    _pdh_close_query,
    _pdh_collect_query_data,
    _pdh_get_formatted_counter_array,
    _pdh_open_query,
    _status_code,
)


PDH_DISK_COUNTERS = {
    "idlePercent": r"\PhysicalDisk(*)\% Idle Time",
    "readBytesPerSecond": r"\PhysicalDisk(*)\Disk Read Bytes/sec",
    "writeBytesPerSecond": r"\PhysicalDisk(*)\Disk Write Bytes/sec",
    "averageResponseSeconds": r"\PhysicalDisk(*)\Avg. Disk sec/Transfer",
    "queueLength": r"\PhysicalDisk(*)\Current Disk Queue Length",
}


def _disk_key(instance_name: str | None) -> str | None:
    if not instance_name:
        return None

    normalized = instance_name.strip().lower()
    if normalized == "_total":
        return None

    match = re.match(r"^(\d+)", normalized)
    if match:
        return f"physicaldrive{match.group(1)}"

    return normalized.replace("\\\\.\\", "")


class PdhDiskSampler:
    def __init__(self) -> None:
        self._query = ctypes.c_void_p()
        self._counters: dict[str, ctypes.c_void_p] = {}
        self._initialized = False
        self._unavailable = False
        self._primed = False
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self._query:
            _pdh_close_query(self._query)
        self._query = ctypes.c_void_p()
        self._counters = {}
        self._initialized = False
        self._primed = False

    def read(self) -> dict[str, dict[str, float]]:
        with self._lock:
            if self._unavailable:
                return {}
            if not self._initialized and not self._initialize():
                return {}

            status = _status_code(_pdh_collect_query_data(self._query))
            if status not in (ERROR_SUCCESS, PDH_INVALID_DATA):
                self._close_unlocked()
                return {}
            if not self._primed:
                self._primed = True
                return {}

            samples = {name: self._read_counter_array(counter) for name, counter in self._counters.items()}

        result: dict[str, dict[str, float]] = {}
        keys = set().union(*(values.keys() for values in samples.values()))
        for key in keys:
            idle = samples.get("idlePercent", {}).get(key)
            read_bps = samples.get("readBytesPerSecond", {}).get(key)
            write_bps = samples.get("writeBytesPerSecond", {}).get(key)
            response_seconds = samples.get("averageResponseSeconds", {}).get(key)
            queue_length = samples.get("queueLength", {}).get(key)
            result[key] = {
                "activeTimePercent": round(max(0.0, min(100.0, 100.0 - idle)), 1) if idle is not None else None,
                "readBytesPerSecond": max(0.0, read_bps or 0.0),
                "writeBytesPerSecond": max(0.0, write_bps or 0.0),
                "averageResponseTimeMs": round(max(0.0, response_seconds or 0.0) * 1000, 1) if response_seconds is not None else None,
                "queueLength": round(max(0.0, queue_length or 0.0), 2) if queue_length is not None else None,
            }
        return result

    def _initialize(self) -> bool:
        status = _status_code(_pdh_open_query(None, 0, ctypes.byref(self._query)))
        if status != ERROR_SUCCESS:
            self._unavailable = True
            return False

        for name, path in PDH_DISK_COUNTERS.items():
            counter = ctypes.c_void_p()
            status = _status_code(_pdh_add_english_counter(
                self._query,
                path,
                0,
                ctypes.byref(counter),
            ))
            if status == ERROR_SUCCESS:
                self._counters[name] = counter

        if not self._counters:
            self._close_unlocked()
            self._unavailable = True
            return False

        _pdh_collect_query_data(self._query)
        self._initialized = True
        return True

    def _read_counter_array(self, counter: ctypes.c_void_p | None) -> dict[str, float]:
        if not counter:
            return {}

        buffer_size = wintypes.DWORD(0)
        item_count = wintypes.DWORD(0)
        status = _status_code(_pdh_get_formatted_counter_array(
            counter,
            PDH_FMT_DOUBLE,
            ctypes.byref(buffer_size),
            ctypes.byref(item_count),
            None,
        ))
        if status != PDH_MORE_DATA or buffer_size.value <= 0 or item_count.value <= 0:
            return {}

        buffer = ctypes.create_string_buffer(buffer_size.value)
        status = _status_code(_pdh_get_formatted_counter_array(
            counter,
            PDH_FMT_DOUBLE,
            ctypes.byref(buffer_size),
            ctypes.byref(item_count),
            ctypes.cast(buffer, ctypes.c_void_p),
        ))
        if status != ERROR_SUCCESS:
            return {}

        items_type = PDH_FMT_COUNTERVALUE_ITEM_W * item_count.value
        items = ctypes.cast(buffer, ctypes.POINTER(items_type)).contents
        values: dict[str, float] = {}
        for item in items:
            if item.FmtValue.CStatus not in (ERROR_SUCCESS, 1):
                continue
            key = _disk_key(item.szName)
            if key:
                values[key] = float(item.FmtValue.doubleValue)
        return values


_sampler = PdhDiskSampler()


def read_disk_counters() -> dict[str, dict[str, Any]]:
    return _sampler.read()

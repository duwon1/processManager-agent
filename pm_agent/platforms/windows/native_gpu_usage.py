"""GPU utilization sampling through the Windows PDH API."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from threading import Lock


ERROR_SUCCESS = 0
PDH_MORE_DATA = 0x800007D2
PDH_INVALID_DATA = 0xC0000BC6
PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_LARGE = 0x00000400
PDH_GPU_ENGINE_COUNTER = r"\GPU Engine(*)\Utilization Percentage"
PDH_GPU_DEDICATED_MEMORY_COUNTER = r"\GPU Adapter Memory(*)\Dedicated Usage"
PDH_GPU_SHARED_MEMORY_COUNTER = r"\GPU Adapter Memory(*)\Shared Usage"


class _PDH_FMT_COUNTERVALUE_UNION(ctypes.Union):
    _fields_ = [
        ("longValue", wintypes.LONG),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("AnsiStringValue", ctypes.c_char_p),
        ("WideStringValue", wintypes.LPWSTR),
    ]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("value", _PDH_FMT_COUNTERVALUE_UNION),
    ]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [
        ("szName", wintypes.LPWSTR),
        ("FmtValue", PDH_FMT_COUNTERVALUE),
    ]


_pdh = ctypes.WinDLL("pdh", use_last_error=True)

_pdh_open_query = _pdh.PdhOpenQueryW
_pdh_open_query.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
_pdh_open_query.restype = wintypes.LONG

_pdh_add_english_counter = _pdh.PdhAddEnglishCounterW
_pdh_add_english_counter.argtypes = [
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_void_p),
]
_pdh_add_english_counter.restype = wintypes.LONG

_pdh_collect_query_data = _pdh.PdhCollectQueryData
_pdh_collect_query_data.argtypes = [ctypes.c_void_p]
_pdh_collect_query_data.restype = wintypes.LONG

_pdh_get_formatted_counter_array = _pdh.PdhGetFormattedCounterArrayW
_pdh_get_formatted_counter_array.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_pdh_get_formatted_counter_array.restype = wintypes.LONG

_pdh_close_query = _pdh.PdhCloseQuery
_pdh_close_query.argtypes = [ctypes.c_void_p]
_pdh_close_query.restype = wintypes.LONG


def _status_code(status: int) -> int:
    return int(status) & 0xFFFFFFFF


class PdhGpuUsageSampler:
    def __init__(self) -> None:
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        self._initialized = False
        self._unavailable = False
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self._query:
            _pdh_close_query(self._query)
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        self._initialized = False

    def read_usage_percent(self) -> float | None:
        with self._lock:
            if self._unavailable:
                return None
            if not self._initialized and not self._initialize():
                return None

            status = _status_code(_pdh_collect_query_data(self._query))
            if status not in (ERROR_SUCCESS, PDH_INVALID_DATA):
                self._close_unlocked()
                return None

            return self._read_counter_array()

    def _initialize(self) -> bool:
        status = _status_code(_pdh_open_query(None, 0, ctypes.byref(self._query)))
        if status != ERROR_SUCCESS:
            self._unavailable = True
            return False

        status = _status_code(_pdh_add_english_counter(
            self._query,
            PDH_GPU_ENGINE_COUNTER,
            0,
            ctypes.byref(self._counter),
        ))
        if status != ERROR_SUCCESS:
            self._close_unlocked()
            self._unavailable = True
            return False

        _pdh_collect_query_data(self._query)
        self._initialized = True
        return True

    def _read_counter_array(self) -> float | None:
        buffer_size = wintypes.DWORD(0)
        item_count = wintypes.DWORD(0)
        status = _status_code(_pdh_get_formatted_counter_array(
            self._counter,
            PDH_FMT_DOUBLE,
            ctypes.byref(buffer_size),
            ctypes.byref(item_count),
            None,
        ))
        if status != PDH_MORE_DATA or buffer_size.value <= 0 or item_count.value <= 0:
            return None

        buffer = ctypes.create_string_buffer(buffer_size.value)
        status = _status_code(_pdh_get_formatted_counter_array(
            self._counter,
            PDH_FMT_DOUBLE,
            ctypes.byref(buffer_size),
            ctypes.byref(item_count),
            ctypes.cast(buffer, ctypes.c_void_p),
        ))
        if status != ERROR_SUCCESS:
            return None

        items_type = PDH_FMT_COUNTERVALUE_ITEM_W * item_count.value
        items = ctypes.cast(buffer, ctypes.POINTER(items_type)).contents
        total = 0.0
        found = False
        for item in items:
            if item.FmtValue.CStatus not in (ERROR_SUCCESS, 1):
                continue
            total += max(0.0, float(item.FmtValue.doubleValue))
            found = True

        if not found:
            return None
        return round(min(total, 100.0), 1)


class PdhGpuMemorySampler:
    def __init__(self) -> None:
        self._query = ctypes.c_void_p()
        self._counters: dict[str, ctypes.c_void_p] = {}
        self._initialized = False
        self._unavailable = False
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

    def read_memory_usage_bytes(self) -> dict[str, list[int]]:
        with self._lock:
            if self._unavailable:
                return {"dedicated": [], "shared": []}
            if not self._initialized and not self._initialize():
                return {"dedicated": [], "shared": []}

            status = _status_code(_pdh_collect_query_data(self._query))
            if status not in (ERROR_SUCCESS, PDH_INVALID_DATA):
                self._close_unlocked()
                return {"dedicated": [], "shared": []}

            return {
                "dedicated": self._read_counter_array(self._counters.get("dedicated")),
                "shared": self._read_counter_array(self._counters.get("shared")),
            }

    def _initialize(self) -> bool:
        status = _status_code(_pdh_open_query(None, 0, ctypes.byref(self._query)))
        if status != ERROR_SUCCESS:
            self._unavailable = True
            return False

        for name, path in {
            "dedicated": PDH_GPU_DEDICATED_MEMORY_COUNTER,
            "shared": PDH_GPU_SHARED_MEMORY_COUNTER,
        }.items():
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

    def _read_counter_array(self, counter: ctypes.c_void_p | None) -> list[int]:
        if not counter:
            return []

        buffer_size = wintypes.DWORD(0)
        item_count = wintypes.DWORD(0)
        status = _status_code(_pdh_get_formatted_counter_array(
            counter,
            PDH_FMT_LARGE,
            ctypes.byref(buffer_size),
            ctypes.byref(item_count),
            None,
        ))
        if status != PDH_MORE_DATA or buffer_size.value <= 0 or item_count.value <= 0:
            return []

        buffer = ctypes.create_string_buffer(buffer_size.value)
        status = _status_code(_pdh_get_formatted_counter_array(
            counter,
            PDH_FMT_LARGE,
            ctypes.byref(buffer_size),
            ctypes.byref(item_count),
            ctypes.cast(buffer, ctypes.c_void_p),
        ))
        if status != ERROR_SUCCESS:
            return []

        items_type = PDH_FMT_COUNTERVALUE_ITEM_W * item_count.value
        items = ctypes.cast(buffer, ctypes.POINTER(items_type)).contents
        values = []
        for item in items:
            if item.FmtValue.CStatus not in (ERROR_SUCCESS, 1):
                continue
            values.append(max(0, int(item.FmtValue.largeValue)))
        return values


_sampler = PdhGpuUsageSampler()
_memory_sampler = PdhGpuMemorySampler()


def read_usage_percent() -> float | None:
    return _sampler.read_usage_percent()


def read_memory_usage_bytes() -> dict[str, list[int]]:
    return _memory_sampler.read_memory_usage_bytes()

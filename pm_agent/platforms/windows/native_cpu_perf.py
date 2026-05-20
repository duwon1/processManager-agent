"""CPU performance counters through the Windows PDH API."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from threading import Lock


ERROR_SUCCESS = 0
PDH_INVALID_DATA = 0xC0000BC6
PDH_CSTATUS_NEW_DATA = 1
PDH_FMT_DOUBLE = 0x00000200

COUNTERS = {
    "utilityPercent": r"\Processor Information(_Total)\% Processor Utility",
    "usagePercent": r"\Processor Information(_Total)\% Processor Time",
    "currentSpeedMhz": r"\Processor Information(_Total)\Processor Frequency",
}


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

_pdh_get_formatted_counter_value = _pdh.PdhGetFormattedCounterValue
_pdh_get_formatted_counter_value.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(PDH_FMT_COUNTERVALUE),
]
_pdh_get_formatted_counter_value.restype = wintypes.LONG

_pdh_close_query = _pdh.PdhCloseQuery
_pdh_close_query.argtypes = [ctypes.c_void_p]
_pdh_close_query.restype = wintypes.LONG


def _status_code(status: int) -> int:
    return int(status) & 0xFFFFFFFF


class PdhCpuPerfSampler:
    def __init__(self) -> None:
        self._query = ctypes.c_void_p()
        self._counters: dict[str, ctypes.c_void_p] = {}
        self._initialized = False
        self._unavailable = False
        self._lock = Lock()

    def read_values(self) -> dict[str, float]:
        with self._lock:
            if self._unavailable:
                return {}
            if not self._initialized and not self._initialize():
                return {}

            status = _status_code(_pdh_collect_query_data(self._query))
            if status not in (ERROR_SUCCESS, PDH_INVALID_DATA):
                self._close_unlocked()
                return {}

            values: dict[str, float] = {}
            for name, counter in self._counters.items():
                value = self._read_counter(counter)
                if value is None:
                    continue
                if name.endswith("Percent"):
                    value = max(0.0, min(value, 100.0))
                values[name] = round(value, 1)
            return values

    def _initialize(self) -> bool:
        status = _status_code(_pdh_open_query(None, 0, ctypes.byref(self._query)))
        if status != ERROR_SUCCESS:
            self._unavailable = True
            return False

        for name, path in COUNTERS.items():
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

    def _read_counter(self, counter: ctypes.c_void_p) -> float | None:
        value = PDH_FMT_COUNTERVALUE()
        counter_type = wintypes.DWORD(0)
        status = _status_code(_pdh_get_formatted_counter_value(
            counter,
            PDH_FMT_DOUBLE,
            ctypes.byref(counter_type),
            ctypes.byref(value),
        ))
        if status == ERROR_SUCCESS and value.CStatus in (ERROR_SUCCESS, PDH_CSTATUS_NEW_DATA):
            return float(value.doubleValue)
        return None

    def _close_unlocked(self) -> None:
        if self._query:
            _pdh_close_query(self._query)
        self._query = ctypes.c_void_p()
        self._counters = {}
        self._initialized = False


_sampler = PdhCpuPerfSampler()


def read_cpu_perf() -> dict[str, float]:
    return _sampler.read_values()

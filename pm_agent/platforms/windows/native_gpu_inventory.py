"""GPU inventory through the Windows DXGI API."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any
from uuid import UUID


DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ADAPTER_FLAG_SOFTWARE = 2


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_uuid(cls, value: str) -> "GUID":
        uuid_value = UUID(value)
        data4 = (ctypes.c_ubyte * 8).from_buffer_copy(uuid_value.bytes[8:])
        return cls(uuid_value.time_low, uuid_value.time_mid, uuid_value.time_hi_version, data4)


class LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", wintypes.DWORD),
        ("HighPart", wintypes.LONG),
    ]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", LUID),
        ("Flags", wintypes.UINT),
    ]


IID_IDXGIFactory1 = GUID.from_uuid("770aae78-f26f-4dba-a829-253c83d1b387")

_dxgi = ctypes.WinDLL("dxgi", use_last_error=True)
_create_dxgi_factory1 = _dxgi.CreateDXGIFactory1
_create_dxgi_factory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_create_dxgi_factory1.restype = wintypes.LONG


def _hresult_code(value: int) -> int:
    return int(value) & 0xFFFFFFFF


def _failed(value: int) -> bool:
    return bool(_hresult_code(value) & 0x80000000)


def _com_method(interface: ctypes.c_void_p, index: int, restype, *argtypes):
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(interface: ctypes.c_void_p | None) -> None:
    if not interface:
        return
    release = _com_method(interface, 2, wintypes.ULONG)
    release(interface)


def _luid_text(value: LUID) -> str:
    high = int(value.HighPart) & 0xFFFFFFFF
    low = int(value.LowPart) & 0xFFFFFFFF
    return f"{high:08x}:{low:08x}"


def _adapter_payload(desc: DXGI_ADAPTER_DESC1) -> dict[str, Any] | None:
    if int(desc.Flags) & DXGI_ADAPTER_FLAG_SOFTWARE:
        return None

    return {
        "model": str(desc.Description).strip() or None,
        "vendorId": int(desc.VendorId),
        "deviceId": int(desc.DeviceId),
        "subSystemId": int(desc.SubSysId),
        "revision": int(desc.Revision),
        "dedicatedMemoryBytes": int(desc.DedicatedVideoMemory),
        "dedicatedSystemMemoryBytes": int(desc.DedicatedSystemMemory),
        "sharedMemoryTotalBytes": int(desc.SharedSystemMemory),
        "adapterLuid": _luid_text(desc.AdapterLuid),
        "source": "dxgi",
    }


def read_gpu_inventory() -> list[dict[str, Any]]:
    factory = ctypes.c_void_p()
    status = _create_dxgi_factory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory))
    if _failed(status) or not factory:
        return []

    adapters: list[dict[str, Any]] = []
    try:
        enum_adapters1 = _com_method(factory, 12, wintypes.LONG, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            status = enum_adapters1(factory, index, ctypes.byref(adapter))
            code = _hresult_code(status)
            if code == DXGI_ERROR_NOT_FOUND:
                break
            if _failed(status) or not adapter:
                break

            try:
                get_desc1 = _com_method(adapter, 10, wintypes.LONG, ctypes.POINTER(DXGI_ADAPTER_DESC1))
                desc = DXGI_ADAPTER_DESC1()
                status = get_desc1(adapter, ctypes.byref(desc))
                if not _failed(status):
                    payload = _adapter_payload(desc)
                    if payload:
                        adapters.append(payload)
            finally:
                _release(adapter)

            index += 1
    finally:
        _release(factory)

    return adapters

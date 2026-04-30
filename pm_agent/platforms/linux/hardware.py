"""Linux 하드웨어 상세 정보 adapter 함수입니다."""
from __future__ import annotations

import platform
import socket
import time
from typing import Any

import psutil

from pm_agent.platforms.linux.capabilities import LINUX_CAPABILITIES
from system import hardware as legacy_hardware


SCHEMA_VERSION = 1


def _parse_size(value: Any) -> int | None:
    """사람이 읽는 크기 문자열을 bytes 숫자로 정규화합니다."""
    text = str(value or "").strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        number = float(text.split()[0])
    except (IndexError, ValueError):
        return None

    upper = text.upper()
    if "TB" in upper:
        return int(number * 1024 ** 4)
    if "GB" in upper:
        return int(number * 1024 ** 3)
    if "MB" in upper:
        return int(number * 1024 ** 2)
    if "KB" in upper:
        return int(number * 1024)
    if " B" in upper or upper.endswith("B"):
        return int(number)
    return None


def _parse_mhz(value: Any) -> float | None:
    """MHz 표시 문자열을 숫자 값으로 정규화합니다."""
    try:
        return float(str(value).replace("MHz", "").strip())
    except (TypeError, ValueError):
        return None


def _item(key: str, value: Any, unit: str = "text") -> dict[str, Any]:
    """OS별 section 항목을 공통 key/value/unit 형식으로 생성합니다."""
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "valueType": "number" if isinstance(value, (int, float)) else "text",
    }


def _section(key: str, items: list[dict[str, Any]], groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """프론트가 한글 라벨을 붙여 렌더링할 section payload를 만듭니다."""
    payload: dict[str, Any] = {"key": key, "items": items}
    if groups:
        payload["groups"] = groups
    return payload


def _uptime_seconds() -> int:
    """부팅 이후 경과 시간을 seconds 단위로 반환합니다."""
    return int(time.time() - psutil.boot_time())


def _disk_groups(disks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """마운트별 디스크 정보를 key-value 그룹으로 변환합니다."""
    groups = []
    for index, disk in enumerate(disks):
        mountpoint = disk.get("mountpoint") or disk.get("device") or f"disk-{index + 1}"
        groups.append({
            "key": f"disk.{index}",
            "titleValue": mountpoint,
            "items": [
                _item("mountpoint", disk.get("mountpoint")),
                _item("device", disk.get("device")),
                _item("filesystem", disk.get("fstype")),
                _item("totalBytes", _parse_size(disk.get("total")), "bytes"),
                _item("usedBytes", _parse_size(disk.get("used")), "bytes"),
                _item("freeBytes", _parse_size(disk.get("free")), "bytes"),
                _item("usagePercent", disk.get("percent"), "percent"),
                _item("diskType", disk.get("type")),
                _item("model", disk.get("model")),
            ],
        })
    return groups


def _network_groups(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """네트워크 어댑터별 정보를 key-value 그룹으로 변환합니다."""
    groups = []
    for index, network in enumerate(networks):
        adapter_name = network.get("adapterName") or f"network-{index + 1}"
        groups.append({
            "key": f"network.{index}",
            "titleValue": adapter_name,
            "items": [
                _item("adapterName", network.get("adapterName")),
                _item("connectionType", network.get("connectionType")),
                _item("ipv4", network.get("ipv4")),
                _item("ipv6", network.get("ipv6")),
                _item("model", network.get("model")),
                _item("ssid", network.get("ssid")),
                _item("signalStrength", network.get("signalStrength")),
            ],
        })
    return groups


def _gpu_groups(gpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """GPU별 정보를 key-value 그룹으로 변환합니다."""
    groups = []
    for index, gpu in enumerate(gpus):
        model = gpu.get("model") or f"gpu-{index + 1}"
        groups.append({
            "key": f"gpu.{index}",
            "titleValue": model,
            "items": [
                _item("model", gpu.get("model")),
                _item("driverVersion", gpu.get("driverVersion")),
                _item("dedicatedMemoryBytes", _parse_size(gpu.get("dedicatedMemory")), "bytes"),
                _item("sharedMemory", gpu.get("sharedMemory")),
            ],
        })
    return groups


def _summary(legacy: dict[str, Any]) -> dict[str, Any]:
    """공통 차트/알림에서 쓰기 쉬운 표준 단위 summary를 생성합니다."""
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu = legacy.get("cpu", {})

    return {
        "cpu": {
            "model": cpu.get("model"),
            "cores": cpu.get("cores"),
            "logicalProcessors": cpu.get("logicalProcessors"),
            "baseSpeedMhz": _parse_mhz(cpu.get("baseSpeedMhz")),
            "uptimeSeconds": _uptime_seconds(),
        },
        "memory": {
            "totalBytes": memory.total,
            "usedBytes": memory.used,
            "availableBytes": memory.available,
            "usagePercent": memory.percent,
            "swapTotalBytes": swap.total,
            "swapUsedBytes": swap.used,
        },
        "disks": [
            {
                "mountpoint": disk.get("mountpoint"),
                "device": disk.get("device"),
                "totalBytes": _parse_size(disk.get("total")),
                "usedBytes": _parse_size(disk.get("used")),
                "freeBytes": _parse_size(disk.get("free")),
                "usagePercent": disk.get("percent"),
            }
            for disk in legacy.get("disks", [])
        ],
        "networks": [
            {
                "adapterName": network.get("adapterName"),
                "ipv4": network.get("ipv4"),
                "connectionType": network.get("connectionType"),
            }
            for network in legacy.get("networks", [])
        ],
    }


def _sections(legacy: dict[str, Any]) -> list[dict[str, Any]]:
    """Linux 전용 상세 정보를 프론트 표시용 section 배열로 구성합니다."""
    cpu = legacy.get("cpu", {})
    memory = legacy.get("memory", {})
    virtual_memory = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return [
        _section("linux.system", [
            _item("hostname", socket.gethostname()),
            _item("kernelSystem", platform.system()),
            _item("kernelRelease", platform.release()),
            _item("kernelVersion", platform.version()),
            _item("architecture", platform.machine()),
            _item("bootTimeEpochSeconds", int(psutil.boot_time()), "epochSeconds"),
            _item("uptimeSeconds", _uptime_seconds(), "seconds"),
        ]),
        _section("linux.cpu", [
            _item("model", cpu.get("model")),
            _item("sockets", cpu.get("sockets"), "count"),
            _item("cores", cpu.get("cores"), "count"),
            _item("logicalProcessors", cpu.get("logicalProcessors"), "count"),
            _item("baseSpeedMhz", _parse_mhz(cpu.get("baseSpeedMhz")), "mhz"),
            _item("currentSpeedMhz", _parse_mhz(cpu.get("currentSpeedMhz")), "mhz"),
            _item("virtualization", cpu.get("virtualization")),
            _item("l1Cache", cpu.get("l1Cache")),
            _item("l2Cache", cpu.get("l2Cache")),
            _item("l3Cache", cpu.get("l3Cache")),
        ]),
        _section("linux.memory", [
            _item("totalBytes", virtual_memory.total, "bytes"),
            _item("usedBytes", virtual_memory.used, "bytes"),
            _item("availableBytes", virtual_memory.available, "bytes"),
            _item("cachedBytes", getattr(virtual_memory, "cached", 0) or 0, "bytes"),
            _item("usagePercent", virtual_memory.percent, "percent"),
            _item("swapTotalBytes", swap.total, "bytes"),
            _item("swapUsedBytes", swap.used, "bytes"),
            _item("speedMhz", memory.get("speedMhz")),
            _item("slotsUsed", memory.get("slotsUsed"), "count"),
            _item("formFactor", memory.get("formFactor")),
        ]),
        _section("linux.disks", [], _disk_groups(legacy.get("disks", []))),
        _section("linux.networks", [], _network_groups(legacy.get("networks", []))),
        _section("linux.gpus", [], _gpu_groups(legacy.get("gpus", []))),
    ]


def collect_hardware() -> dict[str, Any]:
    """Linux 하드웨어 상세 정보에 공통 summary와 OS별 sections를 함께 포함합니다."""
    legacy = legacy_hardware.collect()
    return {
        **legacy,
        "schemaVersion": SCHEMA_VERSION,
        "osType": "Linux",
        "capabilities": LINUX_CAPABILITIES,
        "summary": _summary(legacy),
        "sections": _sections(legacy),
    }

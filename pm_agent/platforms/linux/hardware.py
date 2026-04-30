"""Linux 하드웨어 상세 정보 adapter 함수입니다."""
from __future__ import annotations

import platform
import socket
from typing import Any

import psutil

from pm_agent.platforms.linux.capabilities import LINUX_CAPABILITIES
from system import hardware as legacy_hardware


SCHEMA_VERSION = 1


def _item(key: str, value: Any, unit: str = "text") -> dict[str, Any]:
    """OS별 section 항목을 공통 key/value/unit 형식으로 생성합니다."""
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "valueType": "number" if isinstance(value, (int, float)) else "text",
    }


def _section(key: str, items: list[dict[str, Any]], groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """프론트가 한글 라벨과 표시 단위를 붙여 렌더링할 section payload를 만듭니다."""
    payload: dict[str, Any] = {"key": key, "items": items}
    if groups:
        payload["groups"] = groups
    return payload


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
                _item("totalBytes", disk.get("totalBytes"), "bytes"),
                _item("usedBytes", disk.get("usedBytes"), "bytes"),
                _item("freeBytes", disk.get("freeBytes"), "bytes"),
                _item("usagePercent", disk.get("usagePercent"), "percent"),
                _item("readBytesPerSecond", disk.get("readBytesPerSecond"), "bytesPerSecond"),
                _item("writeBytesPerSecond", disk.get("writeBytesPerSecond"), "bytesPerSecond"),
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
                _item("signalStrengthDbm", network.get("signalStrengthDbm"), "dbm"),
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
                _item("dedicatedMemoryBytes", gpu.get("dedicatedMemoryBytes"), "bytes"),
                _item("usedMemoryBytes", gpu.get("usedMemoryBytes"), "bytes"),
                _item("sharedMemoryBytes", gpu.get("sharedMemoryBytes"), "bytes"),
            ],
        })
    return groups


def _summary(legacy: dict[str, Any]) -> dict[str, Any]:
    """공통 차트/알림에서 쓰기 쉬운 표준 단위 summary를 생성합니다."""
    memory = legacy.get("memory", {})
    cpu = legacy.get("cpu", {})

    return {
        "cpu": {
            "model": cpu.get("model"),
            "cores": cpu.get("cores"),
            "logicalProcessors": cpu.get("logicalProcessors"),
            "baseSpeedMhz": cpu.get("baseSpeedMhz"),
            "uptimeSeconds": cpu.get("uptimeSeconds"),
        },
        "memory": {
            "totalBytes": memory.get("totalBytes"),
            "usedBytes": memory.get("inUseBytes"),
            "availableBytes": memory.get("availableBytes"),
            "usagePercent": memory.get("usagePercent"),
        },
        "disks": [
            {
                "mountpoint": disk.get("mountpoint"),
                "device": disk.get("device"),
                "totalBytes": disk.get("totalBytes"),
                "usedBytes": disk.get("usedBytes"),
                "freeBytes": disk.get("freeBytes"),
                "usagePercent": disk.get("usagePercent"),
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
    """Linux 전용 상세 정보를 표준 단위 section 배열로 구성합니다."""
    cpu = legacy.get("cpu", {})
    memory = legacy.get("memory", {})

    return [
        _section("linux.system", [
            _item("hostname", socket.gethostname()),
            _item("kernelSystem", platform.system()),
            _item("kernelRelease", platform.release()),
            _item("kernelVersion", platform.version()),
            _item("architecture", platform.machine()),
            _item("bootTimeEpochSeconds", int(psutil.boot_time()), "epochSeconds"),
            _item("uptimeSeconds", cpu.get("uptimeSeconds"), "seconds"),
        ]),
        _section("linux.cpu", [
            _item("model", cpu.get("model")),
            _item("sockets", cpu.get("sockets"), "count"),
            _item("cores", cpu.get("cores"), "count"),
            _item("logicalProcessors", cpu.get("logicalProcessors"), "count"),
            _item("baseSpeedMhz", cpu.get("baseSpeedMhz"), "mhz"),
            _item("currentSpeedMhz", cpu.get("currentSpeedMhz"), "mhz"),
            _item("virtualization", cpu.get("virtualization")),
            _item("l1CacheBytes", cpu.get("l1CacheBytes"), "bytes"),
            _item("l2CacheBytes", cpu.get("l2CacheBytes"), "bytes"),
            _item("l3CacheBytes", cpu.get("l3CacheBytes"), "bytes"),
        ]),
        _section("linux.memory", [
            _item("totalBytes", memory.get("totalBytes"), "bytes"),
            _item("usedBytes", memory.get("inUseBytes"), "bytes"),
            _item("availableBytes", memory.get("availableBytes"), "bytes"),
            _item("cachedBytes", memory.get("cachedBytes"), "bytes"),
            _item("committedBytes", memory.get("committedBytes"), "bytes"),
            _item("commitLimitBytes", memory.get("commitLimitBytes"), "bytes"),
            _item("usagePercent", memory.get("usagePercent"), "percent"),
            _item("speedMtPerSecond", memory.get("speedMtPerSecond"), "mtPerSecond"),
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

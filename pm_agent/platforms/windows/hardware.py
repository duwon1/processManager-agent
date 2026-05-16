"""Windows hardware detail collection."""
from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import time
from typing import Any

import psutil

from pm_agent.platforms.windows.capabilities import WINDOWS_CAPABILITIES


SCHEMA_VERSION = 1


def _item(key: str, value: Any, unit: str = "text") -> dict[str, Any]:
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "valueType": "number" if isinstance(value, (int, float)) else "text",
    }


def _section(key: str, items: list[dict[str, Any]], groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"key": key, "items": items}
    if groups:
        payload["groups"] = groups
    return payload


def _run_powershell_json(script: str, timeout: int = 5) -> Any:
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW

    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$OutputEncoding=[System.Text.Encoding]::UTF8; "
        f"{script} | ConvertTo-Json -Compress -Depth 5"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
    except Exception:
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _first_number(*values: Any) -> int | float | None:
    for value in values:
        try:
            if value is not None and str(value).strip() != "":
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _memory_form_factor(code: Any) -> str | None:
    names = {
        8: "DIMM",
        12: "SODIMM",
        13: "SRIMM",
        14: "SMD",
        15: "SSMP",
    }
    try:
        return names.get(int(code))
    except (TypeError, ValueError):
        return None


def _collect_physical_memory() -> dict[str, Any]:
    rows = _as_list(_run_powershell_json(
        "Get-CimInstance Win32_PhysicalMemory | "
        "Select-Object Capacity,Speed,ConfiguredClockSpeed,FormFactor,MemoryType,SMBIOSMemoryType"
    ))
    if not rows:
        return {}

    speeds = [
        _first_number(row.get("ConfiguredClockSpeed"), row.get("Speed"))
        for row in rows
        if isinstance(row, dict)
    ]
    speeds = [speed for speed in speeds if speed]

    form_factors = [
        _memory_form_factor(row.get("FormFactor"))
        for row in rows
        if isinstance(row, dict)
    ]
    form_factors = [form_factor for form_factor in form_factors if form_factor]

    return {
        "speedMtPerSecond": speeds[0] if speeds else None,
        "slotsUsed": len(rows),
        "formFactor": form_factors[0] if form_factors else None,
    }


def _collect_cpu() -> dict[str, Any]:
    freq = psutil.cpu_freq()
    processor = platform.processor() or os.getenv("PROCESSOR_IDENTIFIER") or None
    return {
        "model": processor,
        "sockets": 1,
        "cores": psutil.cpu_count(logical=False) or 1,
        "logicalProcessors": psutil.cpu_count(logical=True) or 1,
        "baseSpeedMhz": round(freq.max, 1) if freq and freq.max else None,
        "currentSpeedMhz": round(freq.current, 1) if freq else None,
        "uptimeSeconds": int(time.time() - psutil.boot_time()),
    }


def _collect_memory() -> dict[str, Any]:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "totalBytes": mem.total,
        "usedBytes": mem.used,
        "availableBytes": mem.available,
        "usagePercent": round(mem.percent, 1),
        "cachedBytes": getattr(mem, "cached", 0) or 0,
        "committedBytes": mem.used + getattr(swap, "used", 0),
        "commitLimitBytes": mem.total + getattr(swap, "total", 0),
        **_collect_physical_memory(),
    }


def _collect_disks() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (PermissionError, OSError):
            continue

        disks.append({
            "mountpoint": partition.mountpoint,
            "device": partition.device,
            "fstype": partition.fstype,
            "totalBytes": usage.total,
            "usedBytes": usage.used,
            "freeBytes": usage.free,
            "usagePercent": round(usage.percent, 1),
            "readBytesPerSecond": None,
            "writeBytesPerSecond": None,
            "type": None,
            "model": None,
        })
    return disks


def _is_loopback(name: str) -> bool:
    normalized = name.strip().lower()
    return "loopback" in normalized or normalized.startswith("lo") or "localhost" in normalized


def _connection_type(name: str) -> str:
    normalized = name.lower()
    if "wi-fi" in normalized or "wifi" in normalized or "wireless" in normalized or "wlan" in normalized:
        return "wifi"
    return "ethernet"


def _collect_networks() -> list[dict[str, Any]]:
    networks: list[dict[str, Any]] = []
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if _is_loopback(name):
            continue
        stat = stats.get(name)
        if stat and not stat.isup:
            continue

        ipv4 = ""
        ipv6 = ""
        for address in addresses:
            if address.family == socket.AF_INET:
                ipv4 = address.address
            elif address.family == socket.AF_INET6:
                ipv6 = address.address

        if not ipv4 and not ipv6:
            continue

        networks.append({
            "adapterName": name,
            "connectionType": _connection_type(name),
            "ipv4": ipv4,
            "ipv6": ipv6,
            "model": None,
            "ssid": None,
            "signalStrengthDbm": None,
        })
    return networks


def _collect_gpus() -> list[dict[str, Any]]:
    rows = _as_list(_run_powershell_json(
        "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterRAM"
    ))
    gpus: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        memory = _first_number(row.get("AdapterRAM"))
        gpus.append({
            "model": row.get("Name"),
            "driverVersion": row.get("DriverVersion"),
            "dedicatedMemoryBytes": memory,
            "usedMemoryBytes": None,
            "sharedMemoryBytes": None,
        })
    return gpus


def _disk_groups(disks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = []
    for index, disk in enumerate(disks):
        title = disk.get("mountpoint") or disk.get("device") or f"disk-{index + 1}"
        groups.append({
            "key": f"disk.{index}",
            "titleValue": title,
            "items": [
                _item("mountpoint", disk.get("mountpoint")),
                _item("device", disk.get("device")),
                _item("filesystem", disk.get("fstype")),
                _item("totalBytes", disk.get("totalBytes"), "bytes"),
                _item("usedBytes", disk.get("usedBytes"), "bytes"),
                _item("freeBytes", disk.get("freeBytes"), "bytes"),
                _item("usagePercent", disk.get("usagePercent"), "percent"),
            ],
        })
    return groups


def _network_groups(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = []
    for index, network in enumerate(networks):
        title = network.get("adapterName") or f"network-{index + 1}"
        groups.append({
            "key": f"network.{index}",
            "titleValue": title,
            "items": [
                _item("adapterName", network.get("adapterName")),
                _item("connectionType", network.get("connectionType")),
                _item("ipv4", network.get("ipv4")),
                _item("ipv6", network.get("ipv6")),
            ],
        })
    return groups


def _gpu_groups(gpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = []
    for index, gpu in enumerate(gpus):
        title = gpu.get("model") or f"gpu-{index + 1}"
        groups.append({
            "key": f"gpu.{index}",
            "titleValue": title,
            "items": [
                _item("model", gpu.get("model")),
                _item("driverVersion", gpu.get("driverVersion")),
                _item("dedicatedMemoryBytes", gpu.get("dedicatedMemoryBytes"), "bytes"),
            ],
        })
    return groups


def _sections(cpu: dict[str, Any], memory: dict[str, Any], disks: list[dict[str, Any]], networks: list[dict[str, Any]], gpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _section("windows.system", [
            _item("hostname", socket.gethostname()),
            _item("kernelSystem", platform.system()),
            _item("kernelRelease", platform.release()),
            _item("kernelVersion", platform.version()),
            _item("architecture", platform.machine()),
            _item("bootTimeEpochSeconds", int(psutil.boot_time()), "epochSeconds"),
            _item("uptimeSeconds", cpu.get("uptimeSeconds"), "seconds"),
        ]),
        _section("windows.cpu", [
            _item("model", cpu.get("model")),
            _item("sockets", cpu.get("sockets"), "count"),
            _item("cores", cpu.get("cores"), "count"),
            _item("logicalProcessors", cpu.get("logicalProcessors"), "count"),
            _item("baseSpeedMhz", cpu.get("baseSpeedMhz"), "mhz"),
            _item("currentSpeedMhz", cpu.get("currentSpeedMhz"), "mhz"),
        ]),
        _section("windows.memory", [
            _item("totalBytes", memory.get("totalBytes"), "bytes"),
            _item("usedBytes", memory.get("usedBytes"), "bytes"),
            _item("availableBytes", memory.get("availableBytes"), "bytes"),
            _item("cachedBytes", memory.get("cachedBytes"), "bytes"),
            _item("committedBytes", memory.get("committedBytes"), "bytes"),
            _item("commitLimitBytes", memory.get("commitLimitBytes"), "bytes"),
            _item("usagePercent", memory.get("usagePercent"), "percent"),
            _item("speedMtPerSecond", memory.get("speedMtPerSecond"), "mtPerSecond"),
            _item("slotsUsed", memory.get("slotsUsed"), "count"),
            _item("formFactor", memory.get("formFactor")),
        ]),
        _section("windows.disks", [], _disk_groups(disks)),
        _section("windows.networks", [], _network_groups(networks)),
        _section("windows.gpus", [], _gpu_groups(gpus)),
    ]


def collect_hardware() -> dict[str, Any]:
    cpu = _collect_cpu()
    memory = _collect_memory()
    disks = _collect_disks()
    networks = _collect_networks()
    gpus = _collect_gpus()

    return {
        "schemaVersion": SCHEMA_VERSION,
        "osType": "Windows",
        "capabilities": WINDOWS_CAPABILITIES,
        "summary": {
            "cpu": {
                "model": cpu.get("model"),
                "cores": cpu.get("cores"),
                "logicalProcessors": cpu.get("logicalProcessors"),
                "baseSpeedMhz": cpu.get("baseSpeedMhz"),
                "uptimeSeconds": cpu.get("uptimeSeconds"),
            },
            "memory": {
                "totalBytes": memory.get("totalBytes"),
                "usedBytes": memory.get("usedBytes"),
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
                for disk in disks
            ],
            "networks": [
                {
                    "adapterName": network.get("adapterName"),
                    "ipv4": network.get("ipv4"),
                    "connectionType": network.get("connectionType"),
                }
                for network in networks
            ],
        },
        "cpu": cpu,
        "memory": memory,
        "disks": disks,
        "networks": networks,
        "gpus": gpus,
        "sections": _sections(cpu, memory, disks, networks, gpus),
    }

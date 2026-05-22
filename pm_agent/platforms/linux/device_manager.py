"""Linux Device Manager-style inventory collection."""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import socket
import subprocess
from datetime import datetime, timezone
from typing import Any

import psutil

from system import hardware as legacy_hardware


SCHEMA_VERSION = 1
NA = "N/A"
MAX_UDEV_DEVICES = 700
MAX_SYS_BUS_DEVICES = 350

LINUX_CATEGORY_LABELS = {
    "Processor": "프로세서",
    "Display": "디스플레이 어댑터",
    "Net": "네트워크 어댑터",
    "DiskDrive": "디스크 드라이브",
    "HDC": "저장소 컨트롤러",
    "USB": "범용 직렬 버스 장치",
    "Bluetooth": "Bluetooth",
    "Media": "사운드, 비디오 및 게임 컨트롤러",
    "System": "시스템 장치",
    "SecurityDevices": "보안 장치",
    "Battery": "배터리",
    "Keyboard": "키보드",
    "Mouse": "마우스 및 기타 포인팅 장치",
    "HIDClass": "휴먼 인터페이스 장치",
    "AudioEndpoint": "오디오 입력 및 출력",
    "Biometric": "생체 인식 장치",
    "Camera": "카메라",
    "CDROM": "DVD/CD-ROM 드라이브",
    "Computer": "컴퓨터",
    "Firmware": "펌웨어",
    "Image": "이미징 장치",
    "Modem": "모뎀",
    "Monitor": "모니터",
    "Ports": "포트(COM & LPT)",
    "Printer": "프린터",
    "PrintQueue": "인쇄 큐",
    "SCSIAdapter": "SCSI 어댑터",
    "Sensor": "센서",
    "SmartCardReader": "스마트 카드 판독기",
    "SoftwareComponent": "소프트웨어 구성 요소",
    "SoftwareDevice": "소프트웨어 장치",
    "USBDevice": "범용 직렬 버스 장치",
    "Volume": "저장소 볼륨",
    "WPD": "휴대용 장치",
}

SYS_BUS_CATEGORY = {
    "acpi": "System",
    "auxiliary": "SoftwareComponent",
    "bluetooth": "Bluetooth",
    "clockevents": "System",
    "clocksource": "System",
    "container": "System",
    "cpu": "Processor",
    "dmi": "Computer",
    "edac": "System",
    "event_source": "System",
    "gpio": "System",
    "hid": "HIDClass",
    "i2c": "System",
    "isa": "System",
    "machinecheck": "System",
    "mdio_bus": "Net",
    "memory": "System",
    "mipi-dsi": "Display",
    "mmc": "DiskDrive",
    "nd": "DiskDrive",
    "nvmem": "Firmware",
    "nvme": "HDC",
    "pci": "System",
    "pci_express": "System",
    "platform": "System",
    "pnp": "System",
    "rapidio": "System",
    "scsi": "SCSIAdapter",
    "sdio": "System",
    "serio": "HIDClass",
    "spi": "System",
    "tee": "SecurityDevices",
    "thunderbolt": "USB",
    "usb": "USBDevice",
    "usb-serial": "Ports",
    "virtio": "System",
    "vmbus": "System",
    "wmi": "System",
}


def _run(cmd: list[str], timeout: float = 4) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            value = file.read().strip()
        return value or None
    except Exception:
        return None


def _clean(value: Any, default: str = NA) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
        return text if text else default
    return value


def _to_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _strip_pci_id(value: str | None) -> str:
    if not value:
        return NA
    return re.sub(r"\s*\[[0-9a-fA-F:.]+\]\s*", " ", value).strip() or NA


def _extract_last_id(value: str | None) -> str:
    if not value:
        return NA
    matches = re.findall(r"\[([0-9a-fA-F]{4}(?::[0-9a-fA-F]{4})?)\]", value)
    return matches[-1] if matches else NA


def _category_label(category: str) -> str:
    return LINUX_CATEGORY_LABELS.get(category, category or "기타 장치")


def _decode_udev_text(value: str | None) -> str | None:
    if not value:
        return None
    text = value.replace("\\x20", " ").replace("\\x2d", "-").replace("_", " ").strip()
    return text or None


def _sys_driver_name(sys_path: str | None) -> str | None:
    if not sys_path:
        return None
    driver_path = os.path.join(sys_path, "driver")
    try:
        if os.path.islink(driver_path):
            return os.path.basename(os.path.realpath(driver_path)) or None
    except Exception:
        return None
    return None


def _sys_attr(sys_path: str | None, *names: str) -> str | None:
    if not sys_path:
        return None
    for name in names:
        value = _read_text(os.path.join(sys_path, name))
        if value:
            return value
    return None


def _device_key_value(device: dict[str, Any]) -> str:
    return str(device.get("pnpDeviceId") or device.get("deviceId") or device.get("name") or "").lower()


def _udev_property(props: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = _decode_udev_text(props.get(name))
        if value:
            return value
    return None


def _udev_name(props: dict[str, str], devname: str | None, sys_path: str | None) -> str:
    return (
        _udev_property(
            props,
            "ID_MODEL_FROM_DATABASE",
            "ID_MODEL",
            "ID_NAME",
            "ID_NET_NAME_ONBOARD",
            "ID_NET_NAME_SLOT",
            "ID_NET_NAME_PATH",
            "ID_FS_LABEL",
            "NAME",
        )
        or _sys_attr(sys_path, "product", "model", "name", "type", "modalias")
        or devname
        or (os.path.basename(sys_path) if sys_path else None)
        or NA
    )


def _udev_manufacturer(props: dict[str, str], sys_path: str | None) -> str:
    return (
        _udev_property(props, "ID_VENDOR_FROM_DATABASE", "ID_VENDOR", "ID_USB_VENDOR", "ID_NET_DRIVER")
        or _sys_attr(sys_path, "manufacturer", "vendor")
        or NA
    )


def _udev_status(sys_path: str | None) -> str:
    state = _sys_attr(sys_path, "state", "operstate")
    if state:
        return state
    return "OK" if sys_path and os.path.exists(sys_path) else NA


def _udev_category(props: dict[str, str], sys_path: str | None, name: str) -> str:
    subsystem = (props.get("SUBSYSTEM") or "").lower()
    devtype = (props.get("DEVTYPE") or "").lower()
    source = f"{subsystem} {devtype} {name} {props.get('ID_INPUT_KEYBOARD', '')} {props.get('ID_INPUT_MOUSE', '')}".lower()

    if props.get("ID_INPUT_KEYBOARD") == "1":
        return "Keyboard"
    if props.get("ID_INPUT_MOUSE") == "1":
        return "Mouse"
    if props.get("ID_INPUT_TOUCHPAD") == "1" or props.get("ID_INPUT_TOUCHSCREEN") == "1" or props.get("ID_INPUT") == "1":
        return "HIDClass"
    if props.get("ID_CDROM") == "1" or devtype == "disk" and props.get("ID_CDROM"):
        return "CDROM"
    if props.get("ID_DRIVE_FLASH_SD") == "1" or props.get("ID_DRIVE_THUMB") == "1":
        return "DiskDrive"
    if props.get("ID_NET_DRIVER") or subsystem == "net":
        return "Net"
    if props.get("ID_BUS") == "usb":
        return "USBDevice"

    if subsystem in {"block", "bdi"}:
        return "Volume" if devtype == "partition" else "DiskDrive"
    if subsystem in {"drm", "graphics", "backlight"}:
        return "Display"
    if subsystem in {"sound"}:
        return "AudioEndpoint"
    if subsystem in {"input", "hid", "i2c"} and any(term in source for term in ("keyboard", "kbd")):
        return "Keyboard"
    if subsystem in {"input", "hid", "i2c"} and "mouse" in source:
        return "Mouse"
    if subsystem in {"input", "hid", "i2c", "serio"}:
        return "HIDClass"
    if subsystem in {"video4linux", "media"}:
        return "Camera" if "camera" in source or "webcam" in source else "Image"
    if subsystem in {"bluetooth"} or "bluetooth" in source:
        return "Bluetooth"
    if subsystem in {"tty"}:
        return "Ports"
    if subsystem in {"usb", "usb-serial"}:
        return "USBDevice"
    if subsystem in {"nvme", "scsi"}:
        return "HDC"
    if subsystem in {"power_supply"}:
        return "Battery"
    if subsystem in {"dmi", "cpu"}:
        return "Computer" if subsystem == "dmi" else "Processor"
    if subsystem in {"tpm", "tee"}:
        return "SecurityDevices"
    if subsystem in {"firmware", "nvmem"}:
        return "Firmware"
    if subsystem in {"leds", "hwmon", "thermal", "watchdog", "rtc", "regulator"}:
        return "System"
    if sys_path:
        for bus, category in SYS_BUS_CATEGORY.items():
            if f"/bus/{bus}/" in sys_path:
                return category
    return "SoftwareDevice" if subsystem in {"module", "drivers"} else "System"


def _kernel_driver_fields(driver: str | None, modules: str | None = None) -> dict[str, Any]:
    driver_value = _clean(driver)
    kernel_version = platform.release() if driver_value != NA else NA
    return {
        "service": driver_value,
        "driverProvider": "Linux kernel" if driver_value != NA else NA,
        "driverVersion": kernel_version or NA,
        "driverInf": NA,
        "driverSigner": NA,
        "kernelModules": _clean(modules),
    }


def _device(
    *,
    name: Any,
    category: str,
    manufacturer: Any = None,
    status: Any = "OK",
    present: bool = True,
    problem_code: int | None = None,
    device_id: Any = None,
    pnp_device_id: Any = None,
    description: Any = None,
    service: Any = None,
    driver_provider: Any = None,
    driver_version: Any = None,
    driver_inf: Any = None,
    driver_signer: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    driver_fields = _kernel_driver_fields(service)
    if driver_provider is not None:
        driver_fields["driverProvider"] = _clean(driver_provider)
    if driver_version is not None:
        driver_fields["driverVersion"] = _clean(driver_version)
    if driver_inf is not None:
        driver_fields["driverInf"] = _clean(driver_inf)
    if driver_signer is not None:
        driver_fields["driverSigner"] = _clean(driver_signer)

    payload = {
        "name": _clean(name),
        "category": category,
        "categoryLabel": _category_label(category),
        "manufacturer": _clean(manufacturer),
        "status": _clean(status),
        "present": present,
        "problemCode": problem_code,
        "hasProblem": problem_code not in (None, 0),
        "deviceId": _clean(device_id or pnp_device_id),
        "pnpDeviceId": _clean(pnp_device_id or device_id),
        "classGuid": NA,
        "description": _clean(description),
        **driver_fields,
    }
    payload.update({key: _clean(value) for key, value in extra.items()})
    return payload


def _read_cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as file:
            content = file.read()
        match = re.search(r"model name\s*:\s*(.+)", content)
        if match:
            return match.group(1).strip()
        match = re.search(r"Hardware\s*:\s*(.+)", content)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return platform.processor() or NA


def _collect_cpu_devices() -> list[dict[str, Any]]:
    freq = psutil.cpu_freq()
    name = _read_cpu_model()
    cpu = _device(
        name=name,
        category="Processor",
        manufacturer=NA,
        status="OK",
        device_id="cpu:0",
        pnp_device_id="CPU\\0",
        description="Linux processor",
        service="kernel",
        socket=NA,
        cores=psutil.cpu_count(logical=False) or 1,
        logicalProcessors=psutil.cpu_count(logical=True) or 1,
        maxClockMhz=round(freq.max, 1) if freq and freq.max else NA,
        currentClockMhz=round(freq.current, 1) if freq and freq.current else NA,
    )
    return [cpu]


def _dmi_value(*names: str) -> str:
    for name in names:
        value = _read_text(f"/sys/class/dmi/id/{name}")
        if value:
            return value
    return NA


def _collect_baseboard() -> dict[str, Any]:
    return {
        "manufacturer": _dmi_value("board_vendor"),
        "product": _dmi_value("board_name", "product_name"),
        "version": _dmi_value("board_version", "product_version"),
        "serialNumber": _dmi_value("board_serial", "product_serial"),
        "status": "OK",
        "computerManufacturer": _dmi_value("sys_vendor"),
        "computerModel": _dmi_value("product_name"),
        "systemType": platform.machine() or NA,
        "biosManufacturer": _dmi_value("bios_vendor"),
        "biosVersion": _dmi_value("bios_version"),
        "biosName": "Linux firmware",
        "biosSerialNumber": _dmi_value("product_serial", "board_serial"),
    }


def _pci_driver_map() -> dict[str, dict[str, str]]:
    drivers: dict[str, dict[str, str]] = {}
    current_addr: str | None = None
    for line in _run(["lspci", "-Dnnk"], timeout=6).splitlines():
        if not line.startswith("\t"):
            current_addr = line.split(" ", 1)[0].strip() if line.strip() else None
            if current_addr:
                drivers.setdefault(current_addr, {})
            continue
        if not current_addr:
            continue
        stripped = line.strip()
        if stripped.startswith("Kernel driver in use:"):
            drivers[current_addr]["driver"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Kernel modules:"):
            drivers[current_addr]["modules"] = stripped.split(":", 1)[1].strip()
    return drivers


def _pci_category(class_name: str, device_name: str) -> str:
    source = f"{class_name} {device_name}".lower()
    if any(term in source for term in ("vga", "3d controller", "display")):
        return "Display"
    if any(term in source for term in ("ethernet", "network", "wireless")):
        return "Net"
    if any(term in source for term in ("audio", "multimedia")):
        return "Media"
    if any(term in source for term in ("usb controller", "usb")):
        return "USB"
    if any(term in source for term in ("sata", "scsi", "raid", "non-volatile memory", "nvme", "storage")):
        return "HDC"
    if any(term in source for term in ("encryption", "tpm", "security")):
        return "SecurityDevices"
    if "bluetooth" in source:
        return "Bluetooth"
    return "System"


def _parse_pci_mm_line(line: str) -> dict[str, str] | None:
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if len(parts) < 4:
        return None
    return {
        "address": parts[0],
        "className": _strip_pci_id(parts[1]),
        "classId": _extract_last_id(parts[1]),
        "vendor": _strip_pci_id(parts[2]),
        "vendorId": _extract_last_id(parts[2]),
        "device": _strip_pci_id(parts[3]),
        "productId": _extract_last_id(parts[3]),
        "subsystem": _strip_pci_id(" ".join(parts[5:7])) if len(parts) >= 7 else NA,
    }


def _collect_pci_devices() -> list[dict[str, Any]]:
    driver_map = _pci_driver_map()
    devices = []
    for line in _run(["lspci", "-Dmmnn"], timeout=6).splitlines():
        row = _parse_pci_mm_line(line)
        if not row:
            continue
        address = row["address"]
        driver = driver_map.get(address, {})
        category = _pci_category(row["className"], row["device"])
        name = f"{row['vendor']} {row['device']}".strip()
        devices.append(_device(
            name=name,
            category=category,
            manufacturer=row["vendor"],
            status="OK",
            device_id=f"pci:{address}",
            pnp_device_id=f"PCI\\{address}",
            description=row["className"],
            service=driver.get("driver"),
            busAddress=address,
            bus="PCI",
            className=row["className"],
            vendorId=row["vendorId"],
            productId=row["productId"],
            subsystem=row["subsystem"],
            kernelModules=driver.get("modules"),
        ))
    return devices


def _collect_usb_devices() -> list[dict[str, Any]]:
    devices = []
    pattern = re.compile(r"Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)")
    for line in _run(["lsusb"], timeout=5).splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        bus, device_no, vendor_id, product_id, name = match.groups()
        category = "Bluetooth" if "bluetooth" in name.lower() else "USB"
        manufacturer = name.split(" ", 1)[0] if name else NA
        devices.append(_device(
            name=name or f"USB device {vendor_id}:{product_id}",
            category=category,
            manufacturer=manufacturer,
            status="OK",
            device_id=f"usb:{bus}:{device_no}",
            pnp_device_id=f"USB\\VID_{vendor_id.upper()}&PID_{product_id.upper()}",
            description="USB device",
            service=NA,
            bus="USB",
            vendorId=vendor_id,
            productId=product_id,
        ))
    return devices


def _lsblk_json() -> list[dict[str, Any]]:
    columns = "NAME,KNAME,TYPE,SIZE,MODEL,VENDOR,SERIAL,TRAN,MOUNTPOINTS,MOUNTPOINT,FSTYPE,ROTA,RM,STATE"
    out = _run(["lsblk", "-J", "-b", "-o", columns], timeout=6)
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    rows = data.get("blockdevices")
    return rows if isinstance(rows, list) else []


def _join_unique(values: list[Any]) -> str:
    result: list[str] = []
    for value in values:
        if isinstance(value, list):
            result.extend(str(item) for item in value if item)
            continue
        if value:
            result.append(str(value))
    seen: list[str] = []
    for value in result:
        if value not in seen:
            seen.append(value)
    return ", ".join(seen) if seen else NA


def _flatten_block_children(row: dict[str, Any]) -> list[dict[str, Any]]:
    children = row.get("children")
    if not isinstance(children, list):
        return []
    result: list[dict[str, Any]] = []
    for child in children:
        if isinstance(child, dict):
            result.append(child)
            result.extend(_flatten_block_children(child))
    return result


def _collect_disk_devices() -> list[dict[str, Any]]:
    rows = _lsblk_json()
    devices = []
    for row in rows:
        if row.get("type") != "disk":
            continue
        children = _flatten_block_children(row)
        mountpoints = _join_unique([
            child.get("mountpoints") if child.get("mountpoints") else child.get("mountpoint")
            for child in children
        ])
        filesystems = _join_unique([child.get("fstype") for child in children])
        name = row.get("kname") or row.get("name")
        model = row.get("model") or row.get("vendor") or name
        rota = _to_bool(row.get("rota"))
        disk_type = "HDD" if rota is True else "SSD" if rota is False else NA
        devices.append(_device(
            name=model,
            category="DiskDrive",
            manufacturer=row.get("vendor"),
            status=row.get("state") or "OK",
            device_id=f"/dev/{name}",
            pnp_device_id=f"BLOCK\\{name}",
            description="Linux block device",
            service=NA,
            bus=row.get("tran") or NA,
            sizeBytes=row.get("size"),
            serialNumber=row.get("serial"),
            transport=row.get("tran"),
            removable=bool(_to_bool(row.get("rm"))),
            diskType=disk_type,
            partitions=_join_unique([child.get("kname") or child.get("name") for child in children]),
            mountpoints=mountpoints,
            filesystem=filesystems,
        ))

    if devices:
        return devices

    fallback = []
    try:
        legacy = legacy_hardware.collect()
        for disk in legacy.get("disks", []):
            fallback.append(_device(
                name=disk.get("model") or disk.get("device"),
                category="DiskDrive",
                manufacturer=NA,
                status="OK",
                device_id=disk.get("device"),
                pnp_device_id=f"BLOCK\\{disk.get('device')}",
                description="Linux block device",
                service=NA,
                sizeBytes=disk.get("totalBytes"),
                partitions=disk.get("partitions"),
                mountpoints=disk.get("mountpoint"),
                filesystem=disk.get("fstype"),
                diskType=disk.get("type"),
            ))
    except Exception:
        return []
    return fallback


def _network_driver(iface: str) -> str:
    uevent = _read_text(f"/sys/class/net/{iface}/device/uevent")
    if not uevent:
        return NA
    for line in uevent.splitlines():
        if line.startswith("DRIVER="):
            return line.split("=", 1)[1].strip() or NA
    return NA


def _collect_network_devices() -> list[dict[str, Any]]:
    stats = psutil.net_if_stats()
    devices = []
    for iface, addresses in psutil.net_if_addrs().items():
        if iface == "lo":
            continue
        stat = stats.get(iface)
        mac = next((
            addr.address for addr in addresses
            if getattr(addr.family, "name", "") in {"AF_LINK", "AF_PACKET"}
        ), None)
        ipv4 = next((addr.address for addr in addresses if addr.family == socket.AF_INET), None)
        ipv6 = next((addr.address.split("%")[0] for addr in addresses if addr.family == socket.AF_INET6), None)
        driver = _network_driver(iface)
        is_wifi = any(token in iface.lower() for token in ("wlan", "wifi", "wlp", "wl"))
        devices.append(_device(
            name=iface,
            category="Net",
            manufacturer=NA,
            status="up" if stat and stat.isup else "down",
            device_id=f"net:{iface}",
            pnp_device_id=f"NET\\{iface}",
            description="Wi-Fi adapter" if is_wifi else "Ethernet adapter",
            service=driver,
            connectionName=iface,
            adapterType="wireless" if is_wifi else "ethernet",
            macAddress=mac,
            speedBitsPerSecond=(stat.speed * 1_000_000) if stat and stat.speed and stat.speed > 0 else NA,
            physicalAdapter=os.path.exists(f"/sys/class/net/{iface}/device"),
            netEnabled=bool(stat and stat.isup),
            ipv4=ipv4,
            ipv6=ipv6,
        ))
    return devices


def _collect_power_devices() -> list[dict[str, Any]]:
    base = "/sys/class/power_supply"
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return []
    devices = []
    for name in names:
        path = os.path.join(base, name)
        if _read_text(os.path.join(path, "type")) != "Battery":
            continue
        model = _read_text(os.path.join(path, "model_name")) or name
        devices.append(_device(
            name=model,
            category="Battery",
            manufacturer=_read_text(os.path.join(path, "manufacturer")),
            status=_read_text(os.path.join(path, "status")) or "OK",
            device_id=f"power:{name}",
            pnp_device_id=f"POWER\\{name}",
            description="Linux battery",
            service=NA,
            serialNumber=_read_text(os.path.join(path, "serial_number")),
            capacityPercent=_read_text(os.path.join(path, "capacity")),
        ))
    return devices


def _collect_input_devices() -> list[dict[str, Any]]:
    try:
        with open("/proc/bus/input/devices", encoding="utf-8", errors="replace") as file:
            blocks = file.read().strip().split("\n\n")
    except Exception:
        return []

    devices = []
    for index, block in enumerate(blocks):
        name_match = re.search(r'N:\s+Name="([^"]+)"', block)
        handlers_match = re.search(r"H:\s+Handlers=(.+)", block)
        name = name_match.group(1).strip() if name_match else ""
        handlers = handlers_match.group(1).strip() if handlers_match else ""
        source = f"{name} {handlers}".lower()
        if not name:
            continue
        if "kbd" in source or "keyboard" in source:
            category = "Keyboard"
        elif "mouse" in source:
            category = "Mouse"
        elif any(token in source for token in ("event", "input", "touch")):
            category = "HIDClass"
        else:
            continue
        devices.append(_device(
            name=name,
            category=category,
            manufacturer=NA,
            status="OK",
            device_id=f"input:{index}",
            pnp_device_id=f"INPUT\\{index}",
            description=handlers or "Linux input device",
            service=NA,
        ))
    return devices


def _parse_udev_export_db() -> list[dict[str, Any]]:
    blocks = []
    current: dict[str, Any] = {"properties": {}, "symlinks": []}
    for raw_line in _run(["udevadm", "info", "--export-db"], timeout=10).splitlines():
        line = raw_line.strip()
        if not line:
            if current.get("path") or current.get("properties"):
                blocks.append(current)
            current = {"properties": {}, "symlinks": []}
            continue
        prefix, _, value = line.partition(": ")
        if prefix == "P":
            current["path"] = value
        elif prefix == "N":
            current["devname"] = f"/dev/{value}" if not value.startswith("/dev/") else value
        elif prefix == "S":
            current.setdefault("symlinks", []).append(value)
        elif prefix == "E" and "=" in value:
            key, prop_value = value.split("=", 1)
            current.setdefault("properties", {})[key] = prop_value
    if current.get("path") or current.get("properties"):
        blocks.append(current)
    return blocks


def _collect_udev_devices() -> list[dict[str, Any]]:
    devices = []
    for block in _parse_udev_export_db():
        if len(devices) >= MAX_UDEV_DEVICES:
            break
        props = block.get("properties") if isinstance(block.get("properties"), dict) else {}
        devpath = str(block.get("path") or props.get("DEVPATH") or "")
        if not devpath:
            continue
        sys_path = devpath if devpath.startswith("/sys/") else f"/sys{devpath}"
        devname = block.get("devname") or props.get("DEVNAME")
        name = _udev_name(props, str(devname) if devname else None, sys_path)
        if name == NA and not devname:
            continue
        category = _udev_category(props, sys_path, name)
        driver = (
            _udev_property(props, "DRIVER", "ID_NET_DRIVER", "ID_USB_DRIVER")
            or _sys_driver_name(sys_path)
        )
        symlinks = block.get("symlinks") if isinstance(block.get("symlinks"), list) else []
        vendor = _udev_manufacturer(props, sys_path)
        product_id = _udev_property(props, "ID_MODEL_ID", "ID_USB_MODEL_ID")
        vendor_id = _udev_property(props, "ID_VENDOR_ID", "ID_USB_VENDOR_ID")
        devices.append(_device(
            name=name,
            category=category,
            manufacturer=vendor,
            status=_udev_status(sys_path),
            device_id=devname or devpath,
            pnp_device_id=f"UDEV\\{devpath}",
            description=_udev_property(props, "ID_MODEL_FROM_DATABASE", "ID_USB_INTERFACES", "MODALIAS") or props.get("SUBSYSTEM"),
            service=driver,
            bus=_udev_property(props, "ID_BUS") or props.get("SUBSYSTEM"),
            subsystem=props.get("SUBSYSTEM"),
            devtype=props.get("DEVTYPE"),
            devname=devname,
            sysPath=sys_path,
            modalias=props.get("MODALIAS"),
            idPath=_udev_property(props, "ID_PATH", "ID_PATH_TAG"),
            serialNumber=_udev_property(props, "ID_SERIAL_SHORT", "ID_SERIAL"),
            vendorId=vendor_id,
            productId=product_id,
            filesystem=_udev_property(props, "ID_FS_TYPE"),
            filesystemLabel=_udev_property(props, "ID_FS_LABEL"),
            symlinks=", ".join(str(item) for item in symlinks[:8]) if symlinks else NA,
        ))
    return devices


def _collect_sys_bus_devices() -> list[dict[str, Any]]:
    root = "/sys/bus"
    try:
        bus_names = sorted(os.listdir(root))
    except Exception:
        return []

    devices = []
    for bus in bus_names:
        category = SYS_BUS_CATEGORY.get(bus)
        if not category:
            continue
        bus_dir = os.path.join(root, bus, "devices")
        try:
            entries = sorted(os.listdir(bus_dir))
        except Exception:
            continue
        for entry in entries:
            if len(devices) >= MAX_SYS_BUS_DEVICES:
                return devices
            sys_path = os.path.join(bus_dir, entry)
            real_path = os.path.realpath(sys_path)
            driver = _sys_driver_name(real_path)
            name = (
                _sys_attr(real_path, "product", "model", "name", "type", "modalias")
                or entry
            )
            devices.append(_device(
                name=name,
                category=category,
                manufacturer=_sys_attr(real_path, "manufacturer", "vendor"),
                status=_udev_status(real_path),
                device_id=real_path,
                pnp_device_id=f"SYSBUS\\{bus}\\{entry}",
                description=f"Linux {bus} bus device",
                service=driver,
                bus=bus,
                sysPath=real_path,
                modalias=_sys_attr(real_path, "modalias"),
            ))
    return devices


def _deduplicate_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for device in devices:
        key = _device_key_value(device)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(device)
    return result


def _build_categories(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        key = str(device.get("category") or "System")
        grouped.setdefault(key, []).append(device)

    categories = []
    for key, rows in grouped.items():
        rows.sort(key=lambda item: str(item.get("name") or "").lower())
        categories.append({
            "key": key,
            "label": _category_label(key),
            "count": len(rows),
            "problemCount": sum(1 for item in rows if item.get("hasProblem")),
            "devices": rows,
        })
    categories.sort(key=lambda item: item["label"].lower())
    return categories


def _core_gpus(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for device in devices:
        if device.get("category") != "Display":
            continue
        rows.append({
            "name": device.get("name"),
            "manufacturer": device.get("manufacturer"),
            "videoProcessor": device.get("name"),
            "adapterRamBytes": NA,
            "videoMode": NA,
            "pnpDeviceId": device.get("pnpDeviceId"),
            "status": device.get("status"),
            "problemCode": device.get("problemCode"),
            "driverProvider": device.get("driverProvider"),
            "driverVersion": device.get("driverVersion"),
            "driverDate": NA,
            "driverInf": device.get("driverInf"),
        })
    return rows


def _core_networks(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for device in devices:
        if device.get("category") != "Net":
            continue
        rows.append({
            "name": device.get("name"),
            "connectionName": device.get("connectionName"),
            "description": device.get("description"),
            "manufacturer": device.get("manufacturer"),
            "adapterType": device.get("adapterType"),
            "macAddress": device.get("macAddress"),
            "speedBitsPerSecond": device.get("speedBitsPerSecond"),
            "physicalAdapter": device.get("physicalAdapter"),
            "netEnabled": device.get("netEnabled"),
            "serviceName": device.get("service"),
            "pnpDeviceId": device.get("pnpDeviceId"),
            "status": device.get("status"),
            "problemCode": device.get("problemCode"),
            "driverProvider": device.get("driverProvider"),
            "driverVersion": device.get("driverVersion"),
            "driverDate": NA,
            "driverInf": device.get("driverInf"),
        })
    return rows


def collect_device_manager() -> dict[str, Any]:
    cpu = _collect_cpu_devices()
    devices = _deduplicate_devices([
        *cpu,
        *_collect_pci_devices(),
        *_collect_usb_devices(),
        *_collect_disk_devices(),
        *_collect_network_devices(),
        *_collect_power_devices(),
        *_collect_input_devices(),
        *_collect_udev_devices(),
        *_collect_sys_bus_devices(),
    ])
    categories = _build_categories(devices)
    problem_devices = [device for device in devices if device.get("hasProblem")]
    gpus = _core_gpus(devices)
    network_adapters = _core_networks(devices)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "supported": True,
        "osType": "Linux",
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "totalDevices": len(devices),
            "problemDevices": len(problem_devices),
            "categoryCount": len(categories),
            "gpuCount": len(gpus),
            "networkAdapterCount": len(network_adapters),
        },
        "cpu": cpu,
        "baseboard": _collect_baseboard(),
        "gpus": gpus,
        "networkAdapters": network_adapters,
        "categories": categories,
        "devices": devices,
    }

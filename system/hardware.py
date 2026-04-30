"""
시스템 하드웨어 정보 수집 모듈.
에이전트는 화면용 단위 문자열을 만들지 않고 bytes, seconds, percent 같은 표준 숫자로 반환합니다.
"""

import os
import re
import socket
import subprocess
import time

import psutil


# ── 유틸 ─────────────────────────────────────────────────────────────────

def _run(cmd, timeout=3) -> str:
    """쉘 명령 실행 후 stdout을 반환합니다. 실패하면 빈 문자열."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _parse_first_int(value: str | None) -> int | None:
    """OS 명령 출력에서 첫 번째 정수를 추출합니다."""
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _cache_size_bytes(level: int) -> int | None:
    """Linux sysfs 캐시 크기(K/M/G suffix)를 bytes 숫자로 정규화합니다."""
    try:
        base = "/sys/devices/system/cpu/cpu0/cache/"
        for entry in os.listdir(base):
            lvl_path = os.path.join(base, entry, "level")
            size_path = os.path.join(base, entry, "size")
            if not (os.path.exists(lvl_path) and os.path.exists(size_path)):
                continue
            with open(lvl_path, encoding="utf-8") as f:
                if f.read().strip() != str(level):
                    continue
            raw = open(size_path, encoding="utf-8").read().strip().upper()
            number = _parse_first_int(raw)
            if number is None:
                return None
            if raw.endswith("G"):
                return number * 1024 ** 3
            if raw.endswith("M"):
                return number * 1024 ** 2
            if raw.endswith("K"):
                return number * 1024
            return number
    except Exception:
        return None
    return None


# ── CPU ──────────────────────────────────────────────────────────────────

def _cpu_proc_info():
    model = "N/A"
    virtualization = "N/A"
    physical_ids = set()
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"model name\s*:\s*(.+)", content)
        if m:
            model = m.group(1).strip()
        fl = re.search(r"flags\s*:\s*(.+)", content)
        if fl:
            flags = fl.group(1)
            virtualization = "available" if ("vmx" in flags or "svm" in flags) else "unavailable"
        physical_ids = set(re.findall(r"physical id\s*:\s*(\d+)", content))
    except Exception:
        pass
    sockets = len(physical_ids) if physical_ids else 1
    return model, virtualization, sockets


def _collect_cpu() -> dict:
    model, virt, sockets = _cpu_proc_info()
    freq = psutil.cpu_freq()
    return {
        "model": model,
        "baseSpeedMhz": round(freq.min, 1) if freq else None,
        "currentSpeedMhz": round(freq.current, 1) if freq else None,
        "sockets": sockets,
        "cores": psutil.cpu_count(logical=False) or 1,
        "logicalProcessors": psutil.cpu_count(logical=True) or 1,
        "virtualization": virt,
        "l1CacheBytes": _cache_size_bytes(1),
        "l2CacheBytes": _cache_size_bytes(2),
        "l3CacheBytes": _cache_size_bytes(3),
        "uptimeSeconds": int(time.time() - psutil.boot_time()),
    }


# ── 메모리 ───────────────────────────────────────────────────────────────

def _dmidecode_memory() -> dict:
    """dmidecode 메모리 정보를 구조화된 숫자 필드로 반환합니다."""
    result = {}
    out = _run(["sudo", "dmidecode", "-t", "memory"], timeout=5)
    if not out:
        return result

    speeds, form_factors, slots_used = [], [], 0
    in_device = False
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Memory Device"):
            in_device = True
        if not in_device:
            continue
        if line.startswith("Speed:") and "Unknown" not in line and "No Module" not in line:
            speed = _parse_first_int(line.split(":", 1)[1].strip())
            if speed is not None:
                speeds.append(speed)
        if line.startswith("Form Factor:") and "Unknown" not in line:
            value = line.split(":", 1)[1].strip()
            if value and value != "Unknown":
                form_factors.append(value)
        if line.startswith("Size:") and "No Module Installed" not in line:
            slots_used += 1

    if speeds:
        result["speedMtPerSecond"] = speeds[0]
    if form_factors:
        result["formFactor"] = form_factors[0]
    if slots_used:
        result["slotsUsed"] = slots_used
    return result


def _collect_memory() -> dict:
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    info = {
        "inUseBytes": mem.used,
        "availableBytes": mem.available,
        "cachedBytes": getattr(mem, "cached", 0) or 0,
        "committedBytes": mem.used + swap.used,
        "commitLimitBytes": mem.total + swap.total,
        "totalBytes": mem.total,
        "usagePercent": mem.percent,
    }
    info.update(_dmidecode_memory())
    return info


# ── 디스크 (다중) ──────────────────────────────────────────────────────────

def _disk_type(device: str) -> str:
    try:
        dev = re.sub(r"\d+$", "", device.split("/")[-1])
        path = f"/sys/block/{dev}/queue/rotational"
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return "HDD" if f.read().strip() == "1" else "SSD"
    except Exception:
        pass
    return "N/A"


def _disk_model(device: str) -> str:
    """물리 디스크 제품명을 /sys/block에서 읽습니다."""
    try:
        dev = re.sub(r"\d+$", "", device.split("/")[-1])
        model_path = f"/sys/block/{dev}/device/model"
        if os.path.exists(model_path):
            return open(model_path, encoding="utf-8").read().strip()
        vendor_path = f"/sys/block/{dev}/device/vendor"
        if os.path.exists(vendor_path):
            return open(vendor_path, encoding="utf-8").read().strip()
    except Exception:
        pass
    return ""


def _collect_disks() -> list:
    """마운트된 파티션 전체를 표준 숫자 필드로 수집합니다."""
    results = []

    io1 = psutil.disk_io_counters(perdisk=True) or {}
    time.sleep(0.5)
    io2 = psutil.disk_io_counters(perdisk=True) or {}

    for p in psutil.disk_partitions():
        if p.fstype in ("", "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup"):
            continue
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue

        dev_name = re.sub(r"\d+$", "", p.device.split("/")[-1])
        entry = {
            "mountpoint": p.mountpoint,
            "device": p.device,
            "fstype": p.fstype,
            "totalBytes": usage.total,
            "usedBytes": usage.used,
            "freeBytes": usage.free,
            "usagePercent": usage.percent,
            "model": _disk_model(p.device),
            "type": _disk_type(p.device),
        }

        d1 = io1.get(dev_name)
        d2 = io2.get(dev_name)
        if d1 and d2:
            entry["readBytesPerSecond"] = max(0, (d2.read_bytes - d1.read_bytes) * 2)
            entry["writeBytesPerSecond"] = max(0, (d2.write_bytes - d1.write_bytes) * 2)

        results.append(entry)

    return results


# ── 네트워크 (다중) ───────────────────────────────────────────────────────

def _net_model(iface: str) -> str:
    """네트워크 어댑터 드라이버/제품명을 읽습니다."""
    try:
        uevent = f"/sys/class/net/{iface}/device/uevent"
        if os.path.exists(uevent):
            for line in open(uevent, encoding="utf-8"):
                if line.startswith("DRIVER="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _collect_networks() -> list:
    """활성 네트워크 어댑터 전체를 수집합니다."""
    results = []
    for iface, addr_list in psutil.net_if_addrs().items():
        if iface == "lo":
            continue
        ipv4 = next(
            (a.address for a in addr_list
             if a.family == socket.AF_INET and not a.address.startswith("127.")),
            None,
        )
        if not ipv4:
            continue

        ipv6 = next(
            (a.address.split("%")[0] for a in addr_list
             if a.family == socket.AF_INET6 and not a.address.startswith("::1")),
            None,
        )
        is_wifi = any(k in iface.lower() for k in ("wlan", "wifi", "wl0", "wlp"))
        entry = {
            "adapterName": iface,
            "ipv4": ipv4,
            "ipv6": ipv6,
            "connectionType": "wifi" if is_wifi else "ethernet",
            "model": _net_model(iface),
        }
        if is_wifi:
            ssid = _run(["iwgetid", "-r"])
            if ssid:
                entry["ssid"] = ssid
            out = _run(["iwconfig", iface])
            m = re.search(r"Signal level=(-\d+)", out)
            if m:
                entry["signalStrengthDbm"] = int(m.group(1))

        results.append(entry)
    return results


# ── GPU (다중) ───────────────────────────────────────────────────────────

def _lspci_gpus() -> list:
    """lspci에서 GPU 목록을 반환합니다."""
    gpus = []
    out = _run(["lspci"])
    for line in out.splitlines():
        lower = line.lower()
        if any(k in lower for k in ("vga", "display", "3d controller")):
            parts = line.split(":", 2)
            model = parts[2].strip() if len(parts) >= 3 else ""
            if model:
                gpus.append(model)
    return gpus


def _collect_gpus() -> list:
    """감지된 GPU 전체를 표준 숫자 필드로 수집합니다."""
    results = []

    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,driver_version",
        "--format=csv,noheader,nounits",
    ])
    if out:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                total_mib = int(parts[1]) if parts[1].isdigit() else 0
                used_mib = int(parts[2]) if parts[2].isdigit() else 0
                results.append({
                    "model": parts[0],
                    "driverVersion": parts[3],
                    "dedicatedMemoryBytes": total_mib * 1024 ** 2 if total_mib else None,
                    "usedMemoryBytes": used_mib * 1024 ** 2 if used_mib else None,
                })
        if results:
            return results

    lspci_models = _lspci_gpus()
    mem = psutil.virtual_memory()
    kernel = _run(["uname", "-r"])

    for i, model in enumerate(lspci_models):
        entry = {"model": model}

        try:
            cards = sorted(c for c in os.listdir("/sys/class/drm/") if re.match(r"^card\d+$", c))
            if i < len(cards):
                vram_path = f"/sys/class/drm/{cards[i]}/device/mem_info_vram_total"
                if os.path.exists(vram_path):
                    with open(vram_path, encoding="utf-8") as f:
                        entry["dedicatedMemoryBytes"] = int(f.read().strip())
        except Exception:
            pass

        if "dedicatedMemoryBytes" not in entry:
            entry["sharedMemoryBytes"] = mem.total
        if kernel:
            entry["driverVersion"] = kernel

        results.append(entry)

    return results if results else [{"model": "N/A"}]


# ── 공개 API ─────────────────────────────────────────────────────────────

def collect() -> dict:
    """시스템 전체 하드웨어 정보를 수집해 dict로 반환합니다."""
    return {
        "cpu": _collect_cpu(),
        "memory": _collect_memory(),
        "disks": _collect_disks(),
        "gpus": _collect_gpus(),
        "networks": _collect_networks(),
    }

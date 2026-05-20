"""
시스템 하드웨어 정보 수집 모듈.
에이전트는 화면용 단위 문자열을 만들지 않고 bytes, seconds, percent 같은 표준 숫자로 반환합니다.
"""

import copy
import os
import re
import shutil
import socket
import subprocess
import time

import psutil

# Linux의 /proc, /sys, dmidecode, lspci 기반 인벤토리 값은 1초 화면 갱신과 분리해 1시간 캐시합니다.
STATIC_CACHE_SECONDS = 60 * 60
_static_cache = {}


# ── 유틸 ─────────────────────────────────────────────────────────────────

def _cached_static(key, loader):
    now = time.time()
    cached = _static_cache.get(key)
    if cached and now - cached[0] < STATIC_CACHE_SECONDS:
        return copy.deepcopy(cached[1])

    value = loader()
    _static_cache[key] = (now, copy.deepcopy(value))
    return value

def _run(cmd, timeout=3) -> str:
    """쉘 명령 실행 후 stdout을 반환합니다. 실패하면 빈 문자열."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _run_privileged(cmd, timeout=3) -> str:
    """root 필요 명령은 root면 직접, 아니면 제한된 sudo로 실행합니다."""
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    return _run(cmd, timeout=timeout)


def _parse_first_int(value: str | None) -> int | None:
    """OS 명령 출력에서 첫 번째 정수를 추출합니다."""
    if not value:
        return None
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else None


def _read_int_file(path: str) -> int | None:
    """숫자만 담긴 sysfs 파일을 int로 읽습니다. 없거나 권한이 없으면 값을 보내지 않습니다."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
        return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
    except Exception:
        return None


def _drm_card_device_dirs() -> list[str]:
    """DRM card 장치 디렉토리를 card 번호 순서대로 반환합니다."""
    try:
        cards = sorted(
            c for c in os.listdir("/sys/class/drm/")
            if re.match(r"^card\d+$", c)
        )
    except Exception:
        return []

    device_dirs = []
    for card in cards:
        device_dir = f"/sys/class/drm/{card}/device"
        if os.path.isdir(device_dir):
            device_dirs.append(device_dir)
    return device_dirs


def _apply_drm_memory_info(entry: dict, device_dir: str) -> None:
    """DRM sysfs가 제공하는 전용/공유 GPU 메모리 정보를 entry에 추가합니다."""
    static_memory = _cached_static(
        ("drm_memory", device_dir),
        lambda: {
            "vramTotal": _read_int_file(os.path.join(device_dir, "mem_info_vram_total")),
            "gttTotal": _read_int_file(os.path.join(device_dir, "mem_info_gtt_total")),
        },
    )
    vram_used = _read_int_file(os.path.join(device_dir, "mem_info_vram_used"))
    gtt_used = _read_int_file(os.path.join(device_dir, "mem_info_gtt_used"))
    vram_total = static_memory.get("vramTotal") if isinstance(static_memory, dict) else None
    gtt_total = static_memory.get("gttTotal") if isinstance(static_memory, dict) else None

    if vram_total is not None:
        entry["dedicatedMemoryBytes"] = vram_total
    if vram_used is not None:
        entry["usedMemoryBytes"] = vram_used
    if gtt_total is not None:
        entry["sharedMemoryBytes"] = gtt_total
    if "usedMemoryBytes" not in entry and gtt_used is not None:
        entry["usedMemoryBytes"] = gtt_used


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
    model, virt, sockets = _cached_static("cpu_proc_info", _cpu_proc_info)
    freq = psutil.cpu_freq()
    return {
        "model": model,
        "baseSpeedMhz": round(freq.min, 1) if freq else None,
        "currentSpeedMhz": round(freq.current, 1) if freq else None,
        "sockets": sockets,
        "cores": psutil.cpu_count(logical=False) or 1,
        "logicalProcessors": psutil.cpu_count(logical=True) or 1,
        "virtualization": virt,
        "l1CacheBytes": _cached_static("cpu_l1_cache", lambda: _cache_size_bytes(1)),
        "l2CacheBytes": _cached_static("cpu_l2_cache", lambda: _cache_size_bytes(2)),
        "l3CacheBytes": _cached_static("cpu_l3_cache", lambda: _cache_size_bytes(3)),
        "uptimeSeconds": int(time.time() - psutil.boot_time()),
    }


# ── 메모리 ───────────────────────────────────────────────────────────────

def _dmidecode_memory() -> dict:
    """dmidecode 메모리 정보를 구조화된 숫자 필드로 반환합니다."""
    result = {}
    dmidecode_bin = shutil.which("dmidecode") or "/usr/sbin/dmidecode"
    out = _run_privileged([dmidecode_bin, "-t", "memory"], timeout=5)
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
    info.update(_cached_static("memory_dmidecode", _dmidecode_memory))
    return info


# ── 디스크 (다중) ──────────────────────────────────────────────────────────

_VIRTUAL_DISK_FSTYPES = {
    "",
    "tmpfs",
    "devtmpfs",
    "squashfs",
    "overlay",
    "proc",
    "sysfs",
    "cgroup",
}


def _unique_text(values: list[str]) -> list[str]:
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _block_name(device: str) -> str:
    if not device:
        return ""
    return os.path.basename(os.path.realpath(device))


def _parent_block_device(device: str) -> str:
    name = _block_name(device)
    if not name:
        return ""

    sys_block = f"/sys/class/block/{name}"
    if os.path.exists(os.path.join(sys_block, "partition")):
        return os.path.basename(os.path.dirname(os.path.realpath(sys_block)))
    return name


def _disk_type(device: str) -> str:
    dev = _parent_block_device(device)
    if not dev:
        return "N/A"
    return _cached_static(("disk_type", dev), lambda: _read_disk_type(dev))


def _read_disk_type(dev: str) -> str:
    try:
        path = f"/sys/block/{dev}/queue/rotational"
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return "HDD" if f.read().strip() == "1" else "SSD"
    except Exception:
        pass
    return "N/A"


def _disk_model(device: str) -> str:
    """물리 디스크 제품명을 /sys/block에서 읽습니다."""
    dev = _parent_block_device(device)
    if not dev:
        return ""
    return _cached_static(("disk_model", dev), lambda: _read_disk_model(dev))


def _read_disk_model(dev: str) -> str:
    try:
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
    """마운트된 파티션을 물리 블록 디바이스 기준으로 묶어 수집합니다."""
    groups = {}

    io1 = psutil.disk_io_counters(perdisk=True) or {}
    time.sleep(1.0)
    io2 = psutil.disk_io_counters(perdisk=True) or {}

    for p in psutil.disk_partitions():
        if p.fstype in _VIRTUAL_DISK_FSTYPES:
            continue
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue

        dev_name = _parent_block_device(p.device)
        if not dev_name:
            continue

        group = groups.setdefault(dev_name, {
            "mountpoints": [],
            "devices": [],
            "fstypes": [],
            "countedDevices": set(),
            "totalBytes": 0,
            "usedBytes": 0,
            "freeBytes": 0,
        })
        group["mountpoints"].append(p.mountpoint)
        group["devices"].append(p.device)
        group["fstypes"].append(p.fstype)
        if p.device not in group["countedDevices"]:
            group["countedDevices"].add(p.device)
            group["totalBytes"] += usage.total
            group["usedBytes"] += usage.used
            group["freeBytes"] += usage.free

    results = []
    for dev_name, group in groups.items():
        total = group["totalBytes"]
        used = group["usedBytes"]
        partitions = ", ".join(_unique_text(group["mountpoints"]))
        entry = {
            "mountpoint": partitions,
            "partitions": partitions,
            "device": f"/dev/{dev_name}",
            "fstype": ", ".join(_unique_text(group["fstypes"])),
            "totalBytes": total,
            "usedBytes": used,
            "freeBytes": group["freeBytes"],
            "usagePercent": round((used / total) * 100, 1) if total else None,
            "model": _disk_model(dev_name),
            "type": _disk_type(dev_name),
        }

        d1 = io1.get(dev_name)
        d2 = io2.get(dev_name)
        if d1 and d2:
            entry["readBytesPerSecond"] = max(0, d2.read_bytes - d1.read_bytes)
            entry["writeBytesPerSecond"] = max(0, d2.write_bytes - d1.write_bytes)

        results.append(entry)

    return results


# ── 네트워크 (다중) ───────────────────────────────────────────────────────

def _net_model(iface: str) -> str:
    """네트워크 어댑터 드라이버/제품명을 읽습니다."""
    return _cached_static(("network_model", iface), lambda: _read_net_model(iface))


def _read_net_model(iface: str) -> str:
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
    return _cached_static("lspci_gpus", _read_lspci_gpus)


def _read_lspci_gpus() -> list:
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


def _nvidia_gpu_static() -> list:
    results = []
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ])
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            total_mib = int(parts[1]) if parts[1].isdigit() else 0
            results.append({
                "model": parts[0],
                "driverVersion": parts[2],
                "dedicatedMemoryBytes": total_mib * 1024 ** 2 if total_mib else None,
            })
    return results


def _nvidia_gpu_used_memory() -> list[int | None]:
    out = _run([
        "nvidia-smi",
        "--query-gpu=memory.used",
        "--format=csv,noheader,nounits",
    ])
    values = []
    for line in out.splitlines():
        raw = line.strip()
        values.append(int(raw) * 1024 ** 2 if raw.isdigit() else None)
    return values


def _collect_gpus() -> list:
    """감지된 GPU 전체를 표준 숫자 필드로 수집합니다."""
    results = []

    static_rows = _cached_static("nvidia_gpu_static", _nvidia_gpu_static)
    if static_rows:
        used_memory = _nvidia_gpu_used_memory()
        for index, row in enumerate(static_rows):
            entry = dict(row)
            if index < len(used_memory):
                entry["usedMemoryBytes"] = used_memory[index]
            results.append(entry)
        return results

    lspci_models = _lspci_gpus()
    drm_device_dirs = _drm_card_device_dirs()
    kernel = _cached_static("kernel_release", lambda: _run(["uname", "-r"]))

    for i, model in enumerate(lspci_models):
        entry = {"model": model}

        if i < len(drm_device_dirs):
            _apply_drm_memory_info(entry, drm_device_dirs[i])
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

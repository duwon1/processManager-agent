"""
시스템 하드웨어 정보 수집 모듈.
디스크·GPU·네트워크는 여러 개를 지원하기 위해 배열로 반환합니다.
"""

import os
import re
import socket
import subprocess
import time

import psutil


# ── 유틸 ─────────────────────────────────────────────────────────────────

def _fmt(b: int) -> str:
    """bytes → 사람이 읽기 쉬운 크기 문자열."""
    if b <= 0:
        return "0 B"
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.0f} MB"
    return f"{b / 1024:.0f} KB"


def _run(cmd, timeout=3) -> str:
    """쉘 명령 실행 후 stdout을 반환합니다. 실패하면 빈 문자열."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ── CPU ──────────────────────────────────────────────────────────────────

def _cpu_proc_info():
    model = "N/A"
    virtualization = "N/A"
    physical_ids = set()
    try:
        with open("/proc/cpuinfo") as f:
            content = f.read()
        m = re.search(r"model name\s*:\s*(.+)", content)
        if m:
            model = m.group(1).strip()
        fl = re.search(r"flags\s*:\s*(.+)", content)
        if fl:
            flags = fl.group(1)
            virtualization = "사용 가능" if ("vmx" in flags or "svm" in flags) else "사용 불가"
        physical_ids = set(re.findall(r"physical id\s*:\s*(\d+)", content))
    except Exception:
        pass
    sockets = len(physical_ids) if physical_ids else 1
    return model, virtualization, sockets


def _cache_size(level: int) -> str:
    try:
        base = "/sys/devices/system/cpu/cpu0/cache/"
        for entry in os.listdir(base):
            lvl_path  = os.path.join(base, entry, "level")
            size_path = os.path.join(base, entry, "size")
            if os.path.exists(lvl_path) and os.path.exists(size_path):
                with open(lvl_path) as f:
                    if f.read().strip() == str(level):
                        with open(size_path) as f2:
                            return f2.read().strip()
    except Exception:
        pass
    return "N/A"


def _collect_cpu() -> dict:
    model, virt, sockets = _cpu_proc_info()
    freq   = psutil.cpu_freq()
    cores  = psutil.cpu_count(logical=False) or 1
    logic  = psutil.cpu_count(logical=True) or 1
    up     = int(time.time() - psutil.boot_time())
    h, rem = divmod(up, 3600)
    mn, s  = divmod(rem, 60)
    return {
        "model":             model,
        "baseSpeedMhz":      f"{freq.min:.0f} MHz"     if freq else "N/A",
        "currentSpeedMhz":   f"{freq.current:.0f} MHz" if freq else "N/A",
        "sockets":           sockets,
        "cores":             cores,
        "logicalProcessors": logic,
        "virtualization":    virt,
        "l1Cache":           _cache_size(1),
        "l2Cache":           _cache_size(2),
        "l3Cache":           _cache_size(3),
        "uptime":            f"{h:02d}:{mn:02d}:{s:02d}",
    }


# ── 메모리 ───────────────────────────────────────────────────────────────

def _dmidecode_memory() -> dict:
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
            v = line.split(":", 1)[1].strip()
            if v and v != "Unknown":
                speeds.append(v)
        if line.startswith("Form Factor:") and "Unknown" not in line:
            v = line.split(":", 1)[1].strip()
            if v and v != "Unknown":
                form_factors.append(v)
        if line.startswith("Size:") and "No Module Installed" not in line:
            slots_used += 1
    if speeds:
        result["speedMhz"] = speeds[0]
    if form_factors:
        result["formFactor"] = form_factors[0]
    if slots_used:
        result["slotsUsed"] = str(slots_used)
    return result


def _collect_memory() -> dict:
    mem  = psutil.virtual_memory()
    swap = psutil.swap_memory()
    info = {
        "inUse":       _fmt(mem.used),
        "available":   _fmt(mem.available),
        "cached":      _fmt(getattr(mem, "cached", 0) or 0),
        "committed":   _fmt(mem.used + swap.used),
        "commitLimit": _fmt(mem.total + swap.total),
    }
    info.update(_dmidecode_memory())
    return info


# ── 디스크 (다중) ──────────────────────────────────────────────────────────

def _disk_type(device: str) -> str:
    try:
        dev = re.sub(r"\d+$", "", device.split("/")[-1])
        path = f"/sys/block/{dev}/queue/rotational"
        if os.path.exists(path):
            with open(path) as f:
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
            return open(model_path).read().strip()
        vendor_path = f"/sys/block/{dev}/device/vendor"
        if os.path.exists(vendor_path):
            return open(vendor_path).read().strip()
    except Exception:
        pass
    return ""


def _collect_disks() -> list:
    """마운트된 파티션 전체를 수집합니다."""
    results = []
    seen_devices = set()

    # 읽기/쓰기 속도 측정 (0.5초 샘플)
    io1 = psutil.disk_io_counters(perdisk=True)
    time.sleep(0.5)
    io2 = psutil.disk_io_counters(perdisk=True)

    for p in psutil.disk_partitions():
        # 가상 파일시스템 제외
        if p.fstype in ("", "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup"):
            continue
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue

        dev_name = re.sub(r"\d+$", "", p.device.split("/")[-1])
        # 같은 물리 디스크의 중복 파티션은 마운트포인트 기준으로 모두 표시
        entry = {
            "mountpoint": p.mountpoint,
            "device":     p.device,
            "fstype":     p.fstype,
            "total":      _fmt(usage.total),
            "used":       _fmt(usage.used),
            "free":       _fmt(usage.free),
            "percent":    usage.percent,
            "model":      _disk_model(p.device),
            "type":       _disk_type(p.device),
        }

        # 읽기/쓰기 속도 (해당 디스크)
        d1 = io1.get(dev_name)
        d2 = io2.get(dev_name)
        if d1 and d2:
            read_bps  = max(0, (d2.read_bytes  - d1.read_bytes)  * 2)
            write_bps = max(0, (d2.write_bytes - d1.write_bytes) * 2)
            entry["readSpeed"]  = _fmt(read_bps)  + "/s"
            entry["writeSpeed"] = _fmt(write_bps) + "/s"

        results.append(entry)

    return results


# ── 네트워크 (다중) ───────────────────────────────────────────────────────

def _net_model(iface: str) -> str:
    """네트워크 어댑터 드라이버/제품명을 읽습니다."""
    try:
        uevent = f"/sys/class/net/{iface}/device/uevent"
        if os.path.exists(uevent):
            for line in open(uevent):
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
            None
        )
        if not ipv4:
            continue

        ipv6 = next(
            (a.address.split("%")[0] for a in addr_list
             if a.family == socket.AF_INET6 and not a.address.startswith("::1")),
            None
        )
        is_wifi = any(k in iface.lower() for k in ("wlan", "wifi", "wl0", "wlp"))
        entry = {
            "adapterName":    iface,
            "ipv4":           ipv4,
            "ipv6":           ipv6,
            "connectionType": "Wi-Fi" if is_wifi else "이더넷",
            "model":          _net_model(iface),
        }
        if is_wifi:
            ssid = _run(["iwgetid", "-r"])
            if ssid:
                entry["ssid"] = ssid
            out = _run(["iwconfig", iface])
            m = re.search(r"Signal level=(-\d+)", out)
            if m:
                entry["signalStrength"] = f"{m.group(1)} dBm"

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
    """감지된 GPU 전체를 수집합니다."""
    results = []

    # NVIDIA (nvidia-smi로 다중 GPU 지원)
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,driver_version",
        "--format=csv,noheader,nounits",
    ])
    if out:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                total = int(parts[1]) if parts[1].isdigit() else 0
                used  = int(parts[2]) if parts[2].isdigit() else 0
                results.append({
                    "model":           parts[0],
                    "driverVersion":   parts[3],
                    "dedicatedMemory": f"{total / 1024:.1f} GB" if total else None,
                    "sharedMemory":    f"{used  / 1024:.1f} GB 사용 중" if used else None,
                })
        if results:
            return results

    # AMD/Intel (lspci 기반)
    lspci_models = _lspci_gpus()
    mem = psutil.virtual_memory()
    kernel = _run(["uname", "-r"])

    for i, model in enumerate(lspci_models):
        entry = {"model": model}

        # AMD — sysfs gpu_busy_percent
        try:
            cards = sorted(c for c in os.listdir("/sys/class/drm/") if re.match(r"^card\d+$", c))
            if i < len(cards):
                vram_path = f"/sys/class/drm/{cards[i]}/device/mem_info_vram_total"
                if os.path.exists(vram_path):
                    with open(vram_path) as f:
                        entry["dedicatedMemory"] = _fmt(int(f.read().strip()))
        except Exception:
            pass

        # Intel 내장 — 공유 메모리 = 시스템 RAM
        if "dedicatedMemory" not in entry:
            entry["sharedMemory"] = _fmt(mem.total)
        if kernel:
            entry["driverVersion"] = kernel

        results.append(entry)

    return results if results else [{"model": "N/A"}]


# ── 공개 API ─────────────────────────────────────────────────────────────

def collect() -> dict:
    """시스템 전체 하드웨어 정보를 수집해 dict로 반환합니다.
    disk·gpu·network는 다중 지원을 위해 배열로 반환합니다.
    """
    return {
        "cpu":      _collect_cpu(),
        "memory":   _collect_memory(),
        "disks":    _collect_disks(),     # list
        "gpus":     _collect_gpus(),      # list
        "networks": _collect_networks(),  # list
    }

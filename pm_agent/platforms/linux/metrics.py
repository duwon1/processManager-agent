"""Linux 메트릭 수집 adapter 함수입니다."""
from __future__ import annotations

from typing import Any

from system import metrics as legacy_metrics


METRIC_SCHEMA = {
    1: ("cpu.usagePercent", "percent"),
    2: ("gpu.usagePercent", "percent"),
    3: ("memory.usagePercent", "percent"),
    4: ("disk.usagePercent", "percent"),
    5: ("network.uploadBytesPerSecond", "bytesPerSecond"),
    6: ("network.downloadBytesPerSecond", "bytesPerSecond"),
    7: ("cpu.currentSpeedMhz", "mhz"),
    8: ("memory.usedBytes", "bytes"),
    9: ("memory.availableBytes", "bytes"),
    10: ("memory.cachedBytes", "bytes"),
    11: ("memory.committedBytes", "bytes"),
    12: ("disk.readBytesPerSecond", "bytesPerSecond"),
    13: ("disk.writeBytesPerSecond", "bytesPerSecond"),
    14: ("memory.hardwareSummary", "text"),
}


def _parse_percent(value: str) -> float | None:
    """문자열 퍼센트 값을 시계열 저장 가능한 숫자로 변환합니다."""
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_bytes(value: str) -> int | None:
    """KB/MB/GB 문자열을 bytes 숫자로 정규화합니다."""
    text = str(value).strip()
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


def _parse_bytes_per_second(value: str) -> int | None:
    """KB/s, MB/s 문자열을 bytes/sec 숫자로 정규화합니다."""
    text = str(value).replace("/s", "").strip()
    return _parse_bytes(text)


def _parse_mhz(value: str) -> float | None:
    """MHz 문자열에서 숫자만 분리합니다."""
    try:
        return float(str(value).replace("MHz", "").strip())
    except (TypeError, ValueError):
        return None


def _raw_value(value: Any, unit: str) -> Any:
    """표시 문자열을 서버/프론트가 계산하기 쉬운 표준 숫자로 보강합니다."""
    if unit == "percent":
        return _parse_percent(value)
    if unit == "bytes":
        return _parse_bytes(value)
    if unit == "bytesPerSecond":
        return _parse_bytes_per_second(value)
    if unit == "mhz":
        return _parse_mhz(value)
    return value


def get_self_ip() -> str:
    """서버에 보고할 Linux 호스트의 로컬 IP를 반환합니다."""
    return legacy_metrics.get_self_ip()


def collect_metrics() -> list[dict[str, Any]]:
    """기존 표시 필드에 표준 key/rawValue/unit을 함께 붙여 Linux 메트릭을 수집합니다."""
    metrics = legacy_metrics.collect_system_metrics()
    for metric in metrics:
        metric_id = metric.get("id")
        key, unit = METRIC_SCHEMA.get(metric_id, (f"metric.{metric_id}", "text"))
        value = metric.get("value")
        raw_value = _raw_value(value, unit)

        metric["key"] = key
        metric["unit"] = unit
        metric["rawValue"] = raw_value
        metric["valueType"] = "number" if isinstance(raw_value, (int, float)) else "text"
    return metrics

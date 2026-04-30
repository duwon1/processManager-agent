"""Linux 파일 시스템 조회 기능입니다."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def list_files(path: str) -> dict[str, Any]:
    """Linux 경로를 기준으로 읽기 전용 파일 목록을 반환합니다."""
    requested_path = str(path or "").strip()
    target = Path(requested_path).expanduser() if requested_path else Path.home()
    if not target.is_absolute():
        target = (Path.home() / target).resolve()
    else:
        target = target.resolve()

    if target.is_dir():
        entries = [
            _file_entry(child)
            for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        ]
        return {
            "path": str(target),
            "parent": str(target.parent) if target.parent != target else "",
            "entries": entries,
            "error": "",
        }

    return {
        "path": str(target),
        "parent": str(target.parent),
        "entries": [],
        "error": "디렉토리가 아닙니다.",
    }


def _file_entry(path: Path) -> dict[str, Any]:
    """파일 하나를 프론트 파일 목록 표시 형식으로 변환합니다."""
    try:
        stat = path.stat()
        return {
            "name": path.name,
            "path": str(path),
            "type": "directory" if path.is_dir() else "file",
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
            "hidden": path.name.startswith("."),
        }
    except OSError:
        return {
            "name": path.name,
            "path": str(path),
            "type": "unknown",
            "size": 0,
            "modified": 0,
            "hidden": path.name.startswith("."),
        }

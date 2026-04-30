"""Linux PTY 터미널 adapter 함수입니다."""
from __future__ import annotations

from typing import Any

from terminal import terminal_manager


def open_session(session_id: str, cols: int, rows: int) -> None:
    """Linux PTY 터미널 세션을 엽니다."""
    terminal_manager.open_session(session_id, cols, rows)


def write(session_id: str, data: str) -> None:
    """Linux PTY 터미널에 입력을 전달합니다."""
    terminal_manager.write(session_id, data)


def resize(session_id: str, cols: int, rows: int) -> None:
    """Linux PTY 터미널 크기를 변경합니다."""
    terminal_manager.resize(session_id, cols, rows)


def close_session(session_id: str) -> None:
    """Linux PTY 터미널 세션을 닫습니다."""
    terminal_manager.close_session(session_id)


def iter_queues() -> list[tuple[str, Any]]:
    """활성 터미널 출력 큐 목록을 반환합니다."""
    return terminal_manager.get_all_queues()


def cleanup_all() -> None:
    """모든 Linux 터미널 세션을 정리합니다."""
    terminal_manager.cleanup_all()

"""Windows ConPTY terminal adapter functions."""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import threading
from pathlib import Path
from typing import Any

try:
    from winpty import PtyProcess
except ImportError as exc:  # pragma: no cover - exercised only on missing optional dependency.
    PtyProcess = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


DEFAULT_COLS = 80
DEFAULT_ROWS = 24
MIN_COLS = 20
MIN_ROWS = 5
MAX_COLS = 240
MAX_ROWS = 80


class WindowsTerminalManager:
    """Manage Windows pseudo terminal sessions backed by ConPTY/pywinpty."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def open_session(
        self,
        session_id: str,
        cols: int = DEFAULT_COLS,
        rows: int = DEFAULT_ROWS,
        shell: str | None = None,
    ) -> None:
        if PtyProcess is None:
            raise RuntimeError(f"pywinpty is required for Windows terminal support: {_IMPORT_ERROR}")

        cols = _clamp_int(cols, MIN_COLS, MAX_COLS, DEFAULT_COLS)
        rows = _clamp_int(rows, MIN_ROWS, MAX_ROWS, DEFAULT_ROWS)

        with self._lock:
            previous = self._sessions.pop(session_id, None)
        self._close_session(previous)

        command = _shell_command(shell)
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        cwd = _home_directory()

        process = PtyProcess.spawn(command, cwd=cwd, env=env, dimensions=(rows, cols))
        queue: asyncio.Queue = asyncio.Queue()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()

        session = {
            "process": process,
            "running": True,
            "queue": queue,
            "loop": loop,
            "command": command,
        }
        reader_thread = threading.Thread(
            target=self._read_loop,
            args=(session_id, session),
            daemon=True,
        )
        session["thread"] = reader_thread

        with self._lock:
            self._sessions[session_id] = session

        reader_thread.start()
        print(f"[터미널] Windows 세션 시작: {session_id} ({Path(command[0]).name}, {cols}x{rows})")

    def write(self, session_id: str, data: str) -> None:
        session = self._get_session(session_id)
        if not session:
            return
        process = session["process"]
        if session["running"] and process.isalive():
            try:
                process.write(data)
            except Exception:
                self.close_session(session_id)

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        session = self._get_session(session_id)
        if not session:
            return
        process = session["process"]
        if session["running"] and process.isalive():
            try:
                process.setwinsize(
                    _clamp_int(rows, MIN_ROWS, MAX_ROWS, DEFAULT_ROWS),
                    _clamp_int(cols, MIN_COLS, MAX_COLS, DEFAULT_COLS),
                )
            except Exception:
                pass

    def close_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        self._close_session(session)
        if session:
            print(f"[터미널] Windows 세션 종료: {session_id}")

    def iter_queues(self) -> list[tuple[str, Any]]:
        with self._lock:
            return [
                (session_id, session["queue"])
                for session_id, session in self._sessions.items()
                if session["running"]
            ]

    def cleanup_all(self) -> None:
        with self._lock:
            session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            self.close_session(session_id)

    def _get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _read_loop(self, session_id: str, session: dict[str, Any]) -> None:
        process = session["process"]
        queue = session["queue"]
        loop = session["loop"]

        def put(text: str) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, text)
            except Exception:
                pass

        try:
            while session["running"] and process.isalive():
                try:
                    data = process.read(4096)
                except EOFError:
                    break
                except Exception as exc:
                    if session["running"]:
                        put(f"\r\n\033[31m[터미널 읽기 오류: {exc}]\033[0m\r\n")
                    break
                if data:
                    put(data)
        finally:
            session["running"] = False
            with self._lock:
                if self._sessions.get(session_id) is session:
                    self._sessions.pop(session_id, None)
            put("\r\n\033[33m[세션이 종료되었습니다]\033[0m\r\n")

    @staticmethod
    def _close_session(session: dict[str, Any] | None) -> None:
        if not session:
            return
        session["running"] = False
        process = session.get("process")
        if process is not None:
            try:
                process.close(force=True)
            except Exception:
                pass


def _clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _home_directory() -> str:
    home = Path.home()
    return str(home if home.exists() else Path.cwd())


def _shell_command(shell: str | None = None) -> list[str]:
    configured = os.getenv("TERMINAL_SHELL", "").strip()
    requested = str(shell or "").strip()
    if not requested and configured:
        return shlex.split(configured, posix=False)
    requested = _normalize_shell(requested)

    if requested == "cmd":
        cmd = shutil.which("cmd.exe") or "cmd.exe"
        return [cmd, "/K", "chcp 65001 > nul"]

    powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if powershell:
        return [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NoExit",
            "-Command",
            (
                "[Console]::InputEncoding = [Console]::OutputEncoding = "
                "[System.Text.UTF8Encoding]::new(); "
                "$OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                "Clear-Host"
            ),
        ]

    cmd = shutil.which("cmd.exe") or "cmd.exe"
    return [cmd, "/K", "chcp 65001 > nul"]


def _normalize_shell(shell: str | None) -> str:
    normalized = str(shell or "").strip().lower()
    if normalized in {"cmd", "cmd.exe"}:
        return "cmd"
    return "powershell"


terminal_manager = WindowsTerminalManager()


def open_session(session_id: str, cols: int, rows: int, shell: str | None = None) -> None:
    terminal_manager.open_session(session_id, cols, rows, shell)


def write(session_id: str, data: str) -> None:
    terminal_manager.write(session_id, data)


def resize(session_id: str, cols: int, rows: int) -> None:
    terminal_manager.resize(session_id, cols, rows)


def close_session(session_id: str) -> None:
    terminal_manager.close_session(session_id)


def iter_queues() -> list[tuple[str, Any]]:
    return terminal_manager.iter_queues()


def cleanup_all() -> None:
    terminal_manager.cleanup_all()

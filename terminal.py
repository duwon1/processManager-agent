"""PTY 기반 터미널 세션을 관리합니다.
각 세션은 별도 스레드에서 PTY 출력을 읽고, asyncio 큐로 전달합니다.
"""
import asyncio
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import threading


class TerminalManager:
    """비동기 환경에서 PTY 터미널 세션을 관리합니다."""

    def __init__(self):
        # session_id -> { 'master_fd', 'pid', 'running', 'queue', 'loop', 'thread' }
        self.sessions = {}
        self._lock = threading.Lock()

    def open_session(self, session_id: str, cols: int = 80, rows: int = 24) -> None:
        """새 PTY 세션을 시작합니다."""
        with self._lock:
            if session_id in self.sessions:
                self._close_session_internal(session_id)

        pid, master_fd = pty.fork()

        if pid == 0:
            # 자식 프로세스: 홈 디렉터리에서 쉘 실행
            os.environ['TERM'] = 'xterm-256color'
            os.environ['LANG'] = 'ko_KR.UTF-8'
            shell = os.environ.get('SHELL', '/bin/bash')
            os.chdir(os.path.expanduser('~'))
            os.execvp(shell, [shell, '--login'])
        else:
            # 부모 프로세스: PTY 크기 설정 및 읽기 스레드 시작
            try:
                winsize = struct.pack('HHHH', rows, cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

            # asyncio.Queue는 thread-safe하지 않으므로 이벤트 루프를 함께 저장합니다.
            # 백그라운드 스레드에서 loop.call_soon_threadsafe()로 안전하게 넣습니다.
            loop = asyncio.get_event_loop()
            queue: asyncio.Queue = asyncio.Queue()
            session = {
                'master_fd': master_fd,
                'pid': pid,
                'running': True,
                'queue': queue,
                'loop': loop,
            }

            reader_thread = threading.Thread(
                target=self._read_loop,
                args=(session_id, session),
                daemon=True,
            )
            session['thread'] = reader_thread

            with self._lock:
                self.sessions[session_id] = session

            reader_thread.start()
            print(f"[터미널] 세션 시작: {session_id} (pid={pid}, {cols}x{rows})")

    def write(self, session_id: str, data: str) -> None:
        """PTY에 키 입력을 전달합니다."""
        session = self.sessions.get(session_id)
        if session and session['running']:
            try:
                os.write(session['master_fd'], data.encode('utf-8'))
            except OSError:
                self.close_session(session_id)

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        """PTY 터미널 크기를 변경합니다."""
        session = self.sessions.get(session_id)
        if session and session['running']:
            try:
                winsize = struct.pack('HHHH', rows, cols, 0, 0)
                fcntl.ioctl(session['master_fd'], termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    def close_session(self, session_id: str) -> None:
        """PTY 세션을 종료합니다."""
        with self._lock:
            self._close_session_internal(session_id)

    def _close_session_internal(self, session_id: str) -> None:
        """락을 이미 획득한 상태에서 세션을 종료합니다."""
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        session['running'] = False
        pid = session.get('pid')
        master_fd = session.get('master_fd')

        if pid and pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass

        print(f"[터미널] 세션 종료: {session_id}")

    def get_all_queues(self) -> list:
        """모든 활성 세션의 (session_id, queue) 목록을 반환합니다."""
        with self._lock:
            return [(sid, s['queue']) for sid, s in self.sessions.items() if s['running']]

    def cleanup_all(self) -> None:
        """모든 세션을 정리합니다."""
        with self._lock:
            session_ids = list(self.sessions.keys())
        for sid in session_ids:
            self.close_session(sid)

    def _read_loop(self, session_id: str, session: dict) -> None:
        """PTY 출력을 지속적으로 읽어서 asyncio 큐에 넣습니다. (스레드에서 실행)"""
        master_fd = session['master_fd']
        queue = session['queue']
        loop = session['loop']

        def put(text: str) -> None:
            """이벤트 루프에 안전하게 데이터를 전달합니다."""
            try:
                loop.call_soon_threadsafe(queue.put_nowait, text)
            except Exception:
                pass

        try:
            while session['running']:
                r, _, _ = select.select([master_fd], [], [], 0.1)
                if r:
                    try:
                        data = os.read(master_fd, 4096)
                        if data:
                            put(data.decode('utf-8', errors='replace'))
                        else:
                            break  # EOF
                    except OSError:
                        break
        except Exception as e:
            print(f"[터미널] 읽기 오류: {e}")
        finally:
            session['running'] = False
            put("\r\n\033[33m[세션이 종료되었습니다]\033[0m\r\n")


# 전역 터미널 매니저 인스턴스
terminal_manager = TerminalManager()

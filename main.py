import asyncio
import json
import os
import pty
import select
import signal
import struct
import fcntl
import termios
import threading
from contextlib import asynccontextmanager

import uvicorn
import websockets
from fastapi import FastAPI, HTTPException

from api import monitoring, process
from config import get_settings

settings = get_settings()

KILL_SUBSCRIPTION_ID = "process-kill-command"


def stomp_frame(command: str, headers: dict, body: str = "") -> str:
    """STOMP 프로토콜 형식의 프레임 문자열을 생성합니다."""
    frame = f"{command}\n"
    for key, value in headers.items():
        frame += f"{key}:{value}\n"
    return frame + "\n" + body + chr(0)


def extract_stomp_body(frame: str) -> str:
    """STOMP MESSAGE 프레임에서 바디 부분을 추출합니다."""
    if "\n\n" not in frame:
        return ""
    return frame.split("\n\n", 1)[1].rstrip("\x00")


class TerminalManager:
    """비동기 환경에서 PTY 터미널 세션을 관리합니다.
    각 세션은 스레드에서 PTY 출력을 읽고, asyncio 큐로 전달합니다.
    """

    def __init__(self):
        # session_id -> { 'master_fd', 'pid', 'running', 'queue', 'thread' }
        self.sessions = {}
        self._lock = threading.Lock()

    def open_session(self, session_id, cols=80, rows=24):
        """새 PTY 세션을 시작합니다."""
        with self._lock:
            if session_id in self.sessions:
                self._close_session_internal(session_id)

        pid, master_fd = pty.fork()

        if pid == 0:
            # 자식 프로세스: 쉘 실행
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
            # 백그라운드 스레드에서 출력을 넣을 때 loop.call_soon_threadsafe()를 사용합니다.
            loop = asyncio.get_event_loop()
            queue = asyncio.Queue()
            session = {
                'master_fd': master_fd,
                'pid': pid,
                'running': True,
                'queue': queue,
                'loop': loop,
            }

            # PTY 출력을 읽는 스레드
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

    def write(self, session_id, data):
        """PTY에 키 입력을 전달합니다."""
        session = self.sessions.get(session_id)
        if session and session['running']:
            try:
                os.write(session['master_fd'], data.encode('utf-8'))
            except OSError:
                self.close_session(session_id)

    def resize(self, session_id, cols, rows):
        """PTY 터미널 크기를 변경합니다."""
        session = self.sessions.get(session_id)
        if session and session['running']:
            try:
                winsize = struct.pack('HHHH', rows, cols, 0, 0)
                fcntl.ioctl(session['master_fd'], termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    def close_session(self, session_id):
        """PTY 세션을 종료합니다."""
        with self._lock:
            self._close_session_internal(session_id)

    def _close_session_internal(self, session_id):
        """락을 이미 획득한 상태에서 세션을 종료합니다."""
        session = self.sessions.pop(session_id, None)
        if not session:
            return

        session['running'] = False
        pid = session.get('pid')
        master_fd = session.get('master_fd')

        # 자식 프로세스 종료
        if pid and pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        # 마스터 FD 닫기
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass

        print(f"[터미널] 세션 종료: {session_id}")

    def get_all_queues(self):
        """모든 활성 세션의 (session_id, queue) 목록을 반환합니다."""
        with self._lock:
            return [(sid, s['queue']) for sid, s in self.sessions.items() if s['running']]

    def cleanup_all(self):
        """모든 세션을 정리합니다."""
        with self._lock:
            session_ids = list(self.sessions.keys())
        for sid in session_ids:
            self.close_session(sid)

    def _read_loop(self, session_id, session):
        """PTY 출력을 지속적으로 읽어서 asyncio 큐에 넣습니다. (스레드에서 실행)
        asyncio.Queue는 thread-safe하지 않으므로 loop.call_soon_threadsafe()로 넣습니다.
        """
        master_fd = session['master_fd']
        queue = session['queue']
        loop = session['loop']

        def put(text):
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
                            text = data.decode('utf-8', errors='replace')
                            put(text)
                        else:
                            break  # EOF
                    except OSError:
                        break
        except Exception as e:
            print(f"[터미널] 읽기 오류: {e}")
        finally:
            session['running'] = False
            put("\r\n\033[33m[세션이 종료되었습니다]\033[0m\r\n")


# 전역 터미널 매니저
terminal_manager = TerminalManager()


async def run_agent(url: str, account_token: str, hostname: str, os_type: str) -> None:
    """단일 WebSocket 연결로 모니터링·프로세스 전송·kill 명령·터미널을 모두 처리합니다.
    연결이 끊기면 5초 후 자동으로 재연결합니다.
    """
    self_ip = monitoring.get_self_ip()
    print(f"[에이전트] STOMP 연결 시도: {url}")

    while True:
        try:
            async with websockets.connect(url) as websocket:
                # STOMP CONNECT
                await websocket.send(stomp_frame(
                    "CONNECT",
                    {
                        "accept-version": "1.1,1.2",
                        "host": "localhost",
                        "account-token": account_token,
                        "hostname": hostname,
                        "os-type": os_type,
                        "self-ip": self_ip,
                    },
                ))
                resp = await websocket.recv()
                if not str(resp).startswith("CONNECTED"):
                    raise RuntimeError(f"STOMP CONNECT 실패: {resp}")
                print("[에이전트] STOMP 연결 성공")

                # 에이전트 커맨드 채널 구독 (kill + 터미널 명령)
                await websocket.send(stomp_frame(
                    "SUBSCRIBE",
                    {
                        "id": KILL_SUBSCRIPTION_ID,
                        "destination": "/topic/agent.command",
                        "ack": "auto",
                    },
                ))
                print("[에이전트] 명령 채널 구독 시작 (kill + 터미널)")

                async def send_monitoring_loop():
                    """시스템 메트릭을 2초 간격으로 전송합니다."""
                    while True:
                        data = monitoring.collect_system_metrics()
                        await websocket.send(stomp_frame(
                            "SEND",
                            {"destination": "/app/monitoring", "content-type": "application/json"},
                            json.dumps(data),
                        ))
                        await asyncio.sleep(2)

                async def send_process_loop():
                    """프로세스 목록을 2초 간격으로 전송합니다."""
                    while True:
                        data = process.get_process_data()
                        await websocket.send(stomp_frame(
                            "SEND",
                            {"destination": "/app/process", "content-type": "application/json"},
                            json.dumps(data),
                        ))
                        await asyncio.sleep(2)

                async def send_terminal_output_loop():
                    """모든 활성 터미널 세션의 PTY 출력을 STOMP으로 전송합니다."""
                    while True:
                        queues = terminal_manager.get_all_queues()
                        for session_id, queue in queues:
                            # 큐에 쌓인 출력을 모두 꺼내서 한 번에 전송
                            chunks = []
                            while not queue.empty():
                                try:
                                    chunks.append(queue.get_nowait())
                                except asyncio.QueueEmpty:
                                    break
                            if chunks:
                                combined = "".join(chunks)
                                await websocket.send(stomp_frame(
                                    "SEND",
                                    {"destination": "/app/terminal.output", "content-type": "application/json"},
                                    json.dumps({
                                        "sessionId": session_id,
                                        "nodeId": None,
                                        "data": combined,
                                    }),
                                ))
                        await asyncio.sleep(0.05)  # 50ms 간격으로 폴링 (체감 지연 최소화)

                async def receive_commands_loop():
                    """백엔드에서 오는 kill 명령과 터미널 명령을 수신하고 처리합니다."""
                    while True:
                        frame = await websocket.recv()
                        frame_text = str(frame)
                        # MESSAGE 프레임만 처리합니다.
                        if not frame_text.startswith("MESSAGE"):
                            continue

                        body = extract_stomp_body(frame_text)
                        if not body:
                            continue

                        try:
                            payload = json.loads(body)
                        except json.JSONDecodeError:
                            print(f"[에이전트] JSON 파싱 실패: {body}")
                            continue

                        cmd_type = payload.get("type", "")

                        # ── 터미널 명령 처리 ──
                        if cmd_type.startswith("terminal-"):
                            handle_terminal_command(payload, cmd_type)
                            continue

                        # ── 기존 kill 명령 처리 ──
                        # 이 노드가 대상이 아니면 무시합니다.
                        if payload.get("nodeName") != hostname:
                            continue

                        pid = int(payload.get("pid", 0))
                        request_id = str(payload.get("requestId", "")).strip()
                        if not request_id or pid <= 0:
                            continue

                        # 프로세스를 종료하고 결과를 백엔드로 반환합니다.
                        try:
                            message = process.kill_process_by_pid(pid)
                            success = True
                        except HTTPException as exc:
                            message = exc.detail
                            success = False

                        await websocket.send(stomp_frame(
                            "SEND",
                            {"destination": "/app/process/kill-result", "content-type": "application/json"},
                            json.dumps({
                                "requestId": request_id,
                                "pid": pid,
                                "success": success,
                                "message": message,
                                "nodeName": hostname,
                            }),
                        ))

                        # 종료 후 프로세스 목록을 즉시 갱신하여 화면에 반영합니다.
                        fresh = process.get_process_data()
                        await websocket.send(stomp_frame(
                            "SEND",
                            {"destination": "/app/process", "content-type": "application/json"},
                            json.dumps(fresh),
                        ))

                def handle_terminal_command(payload, cmd_type):
                    """터미널 관련 명령을 분기 처리합니다.
                    nodeName이 자신의 hostname과 일치하는 경우에만 처리합니다.
                    """
                    # 다른 노드로 향하는 터미널 명령은 무시합니다.
                    if payload.get("nodeName") and payload.get("nodeName") != hostname:
                        return

                    session_id = payload.get("sessionId", "")

                    if cmd_type == "terminal-open":
                        cols = payload.get("cols", 80)
                        rows = payload.get("rows", 24)
                        terminal_manager.open_session(session_id, cols, rows)

                    elif cmd_type == "terminal-input":
                        data = payload.get("data", "")
                        terminal_manager.write(session_id, data)

                    elif cmd_type == "terminal-resize":
                        cols = payload.get("cols", 80)
                        rows = payload.get("rows", 24)
                        terminal_manager.resize(session_id, cols, rows)

                    elif cmd_type == "terminal-close":
                        terminal_manager.close_session(session_id)

                # 네 루프를 단일 연결에서 동시에 실행합니다.
                await asyncio.gather(
                    send_monitoring_loop(),
                    send_process_loop(),
                    send_terminal_output_loop(),
                    receive_commands_loop(),
                )

        except Exception as e:
            print(f"[에이전트] 연결 에러 (5초 후 재시도): {e}")
            # 연결 끊기면 모든 터미널 세션 정리
            terminal_manager.cleanup_all()
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 생명주기에 맞춰 에이전트 태스크를 시작하고 종료합니다."""
    print("에이전트 기동이 완료되었습니다.")
    print(f"WebSocket 대상 서버: {settings.websocket_url}")
    print(f"에이전트 호스트명: {settings.hostname}")

    # 단일 WebSocket 연결로 모든 통신을 처리하는 태스크를 시작합니다.
    agent_task = asyncio.create_task(
        run_agent(settings.websocket_url, settings.account_token, settings.hostname, settings.os_type)
    )
    yield

    print("에이전트 종료를 시작합니다.")
    terminal_manager.cleanup_all()
    agent_task.cancel()
    try:
        await agent_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)

app.include_router(process.router)
app.include_router(monitoring.router)


@app.get("/monitoring")
def get_http_monitoring():
    """현재 시스템 메트릭을 HTTP로 즉시 조회합니다. 디버깅용으로 사용합니다."""
    return monitoring.collect_system_metrics()


if __name__ == "__main__":
    # reload는 개발 환경에서만 활성화하고 운영에서는 false로 유지합니다.
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=settings.reload)

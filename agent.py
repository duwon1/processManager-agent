"""STOMP WebSocket 에이전트 루프입니다.
모니터링·프로세스·서비스·하드웨어 정보·터미널 데이터를 단일 연결로 처리하며, 끊기면 자동 재연결합니다.
"""
import asyncio
import json

import websockets
from fastapi import HTTPException

from stomp import stomp_frame, extract_stomp_body, extract_stomp_destination
from terminal import terminal_manager
from system import metrics, process, hardware, services

COMMAND_SUBSCRIPTION_ID = "agent-command-channel"
SYSINFO_SUBSCRIPTION_ID = "sysinfo-request-channel"


async def run_agent(url: str, account_token: str, hostname: str, os_type: str) -> None:
    """단일 WebSocket 연결로 모니터링·프로세스 전송·kill 명령·터미널을 모두 처리합니다."""
    self_ip = metrics.get_self_ip()
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

                # 에이전트 커맨드 채널 구독 (kill + 터미널 + 서비스 제어)
                await websocket.send(stomp_frame(
                    "SUBSCRIBE",
                    {
                        "id": COMMAND_SUBSCRIPTION_ID,
                        "destination": "/topic/agent.command",
                        "ack": "auto",
                    },
                ))

                # 시스템 정보 수집 요청 채널 구독
                await websocket.send(stomp_frame(
                    "SUBSCRIBE",
                    {
                        "id": SYSINFO_SUBSCRIPTION_ID,
                        "destination": "/topic/agent.sysinfo-request",
                        "ack": "auto",
                    },
                ))
                print("[에이전트] 시스템 정보 요청 채널 구독 시작")

                async def send_monitoring_loop():
                    """시스템 메트릭을 2초 간격으로 전송합니다."""
                    while True:
                        data = metrics.collect_system_metrics()
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

                async def send_service_loop():
                    """서비스 목록을 10초 간격으로 전송합니다."""
                    while True:
                        try:
                            svc_list = services.get_service_list()
                            await websocket.send(stomp_frame(
                                "SEND",
                                {"destination": "/app/service", "content-type": "application/json"},
                                json.dumps(svc_list),
                            ))
                        except Exception as e:
                            print(f"[에이전트] 서비스 목록 전송 오류: {e}")
                        await asyncio.sleep(10)

                async def send_terminal_output_loop():
                    """모든 활성 터미널 세션의 PTY 출력을 STOMP으로 전송합니다."""
                    while True:
                        for session_id, queue in terminal_manager.get_all_queues():
                            chunks = []
                            while not queue.empty():
                                try:
                                    chunks.append(queue.get_nowait())
                                except asyncio.QueueEmpty:
                                    break
                            if chunks:
                                await websocket.send(stomp_frame(
                                    "SEND",
                                    {"destination": "/app/terminal.output", "content-type": "application/json"},
                                    json.dumps({
                                        "sessionId": session_id,
                                        "nodeId": None,
                                        "data": "".join(chunks),
                                    }),
                                ))
                        await asyncio.sleep(0.05)

                async def receive_commands_loop():
                    """백엔드에서 오는 명령(kill·터미널·시스템 정보·서비스 제어)을 수신하고 처리합니다."""
                    while True:
                        frame = await websocket.recv()
                        frame_text = str(frame)
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

                        destination = extract_stomp_destination(frame_text)

                        # ── 시스템 정보 수집 요청 ──
                        if destination == "/topic/agent.sysinfo-request":
                            if payload.get("nodeName") == hostname:
                                try:
                                    loop = asyncio.get_event_loop()
                                    info = await loop.run_in_executor(None, hardware.collect)
                                    info["nodeId"] = payload.get("nodeId")
                                    await websocket.send(stomp_frame(
                                        "SEND",
                                        {"destination": "/app/system-info", "content-type": "application/json"},
                                        json.dumps(info),
                                    ))
                                    print("[에이전트] 시스템 정보 전송 완료")
                                except Exception as e:
                                    print(f"[에이전트] 시스템 정보 수집 오류: {e}")
                            continue

                        cmd_type = payload.get("type", "")

                        # ── 서비스 제어 명령 ──
                        if cmd_type == "service-control":
                            if payload.get("nodeName") != hostname:
                                continue
                            svc_name = payload.get("name", "")
                            action = payload.get("action", "")
                            try:
                                message = services.control_service(svc_name, action)
                                success = True
                            except Exception as e:
                                message = str(e)
                                success = False
                            await websocket.send(stomp_frame(
                                "SEND",
                                {"destination": "/app/service-control-result", "content-type": "application/json"},
                                json.dumps({
                                    "name": svc_name,
                                    "action": action,
                                    "success": success,
                                    "message": message,
                                    "nodeName": hostname,
                                }),
                            ))
                            continue

                        # ── 언인스톨 명령 처리 ──
                        if cmd_type == "uninstall":
                            if payload.get("nodeName") == hostname:
                                print("[에이전트] 언인스톨 명령 수신 → 자가 삭제 시작")
                                import subprocess
                                subprocess.Popen([
                                    'bash', '-c',
                                    'sleep 2 && sudo systemctl disable processmanager-agent && '
                                    'sudo rm -rf /opt/processmanager-agent && '
                                    'sudo rm -f /etc/systemd/system/processmanager-agent.service && '
                                    'sudo systemctl daemon-reload && '
                                    'sudo systemctl stop processmanager-agent'
                                ])
                                raise SystemExit(0)
                            continue

                        # ── 터미널 명령 처리 ──
                        if cmd_type.startswith("terminal-"):
                            _handle_terminal_command(payload, cmd_type, hostname)
                            continue

                        # ── kill 명령 처리 ──
                        if payload.get("nodeName") != hostname:
                            continue

                        pid = int(payload.get("pid", 0))
                        request_id = str(payload.get("requestId", "")).strip()
                        if not request_id or pid <= 0:
                            continue

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

                        fresh = process.get_process_data()
                        await websocket.send(stomp_frame(
                            "SEND",
                            {"destination": "/app/process", "content-type": "application/json"},
                            json.dumps(fresh),
                        ))

                # 다섯 루프를 단일 연결에서 동시에 실행합니다.
                await asyncio.gather(
                    send_monitoring_loop(),
                    send_process_loop(),
                    send_service_loop(),
                    send_terminal_output_loop(),
                    receive_commands_loop(),
                )

        except Exception as e:
            print(f"[에이전트] 연결 에러 (5초 후 재시도): {e}")
            terminal_manager.cleanup_all()
            await asyncio.sleep(5)


def _handle_terminal_command(payload: dict, cmd_type: str, hostname: str) -> None:
    """터미널 관련 명령을 분기 처리합니다."""
    if payload.get("nodeName") and payload.get("nodeName") != hostname:
        return

    session_id = payload.get("sessionId", "")

    if cmd_type == "terminal-open":
        terminal_manager.open_session(session_id, payload.get("cols", 80), payload.get("rows", 24))
    elif cmd_type == "terminal-input":
        terminal_manager.write(session_id, payload.get("data", ""))
    elif cmd_type == "terminal-resize":
        terminal_manager.resize(session_id, payload.get("cols", 80), payload.get("rows", 24))
    elif cmd_type == "terminal-close":
        terminal_manager.close_session(session_id)
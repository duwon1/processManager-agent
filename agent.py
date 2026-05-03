"""STOMP WebSocket 에이전트 루프입니다.
모니터링·프로세스·서비스·하드웨어 정보·터미널 데이터를 단일 연결로 처리하며, 끊기면 자동 재연결합니다.
"""
import asyncio
import json
import os
import subprocess

import websockets
from fastapi import HTTPException

from pm_agent.platforms.factory import get_platform_adapter
from stomp import stomp_frame, extract_stomp_body, extract_stomp_destination

COMMAND_SUBSCRIPTION_ID = "agent-command-channel"
SYSINFO_SUBSCRIPTION_ID = "sysinfo-request-channel"
UPDATE_CHECK_INTERVAL_SECONDS = 10 * 60


def get_git_revisions(agent_dir: str) -> tuple[str, str, str]:
    """로컬/원격 Git 커밋을 조회해 에이전트 업데이트 필요 여부 판단에 사용합니다."""
    try:
        current_sha = subprocess.check_output(
            ["git", "-C", agent_dir, "rev-parse", "HEAD"],
            text=True,
            timeout=5,
        ).strip()[:7]
    except Exception as exc:
        current_sha = "unknown"
        return current_sha, current_sha, str(exc)

    try:
        result = subprocess.check_output(
            ["git", "-C", agent_dir, "ls-remote", "origin", "HEAD"],
            text=True,
            timeout=10,
        )
        latest_sha = result.split()[0][:7] if result.strip() else current_sha
        return current_sha, latest_sha, ""
    except Exception as exc:
        return current_sha, current_sha, str(exc)


async def run_agent(
    url: str,
    account_token: str,
    hostname: str,
    os_type: str,
    agent_id: str = "",
    service_name: str = "processmanager-agent",
    agent_secret: str = "",
) -> None:
    """단일 WebSocket 연결로 모니터링·프로세스 전송·kill 명령·터미널을 모두 처리합니다."""
    # 서버 통신은 공통으로 유지하고, OS 의존 기능은 adapter를 통해 실행합니다.
    platform_adapter = get_platform_adapter(os_type)
    self_ip = platform_adapter.get_self_ip()
    update_lock = asyncio.Lock()
    print(f"[에이전트] STOMP 연결 시도: {url}")

    while True:
        try:
            async with websockets.connect(url) as websocket:
                # 등록된 노드는 agent-secret을 우선 사용하고, 최초 등록/재설치는 account-token을 사용합니다.
                connect_headers = {
                    "accept-version": "1.1,1.2",
                    "host": "localhost",
                    "hostname": hostname,
                    "os-type": os_type,
                    "agent-id": agent_id,
                    "self-ip": self_ip,
                }
                if getattr(platform_adapter, "capabilities", None):
                    # 서버가 아직 capability를 저장하지 않아도 무시 가능한 보조 헤더로 전송합니다.
                    connect_headers["capabilities"] = json.dumps(platform_adapter.capabilities, separators=(",", ":"))
                if agent_secret:
                    connect_headers["agent-secret"] = agent_secret
                else:
                    connect_headers["account-token"] = account_token
                await websocket.send(stomp_frame("CONNECT", connect_headers))
                resp = await websocket.recv()
                if not str(resp).startswith("CONNECTED"):
                    raise RuntimeError(f"STOMP CONNECT 실패: {resp}")
                print("[에이전트] STOMP 연결 성공")

                # 에이전트 커맨드 채널 구독 (kill + 터미널 + 서비스 제어)
                await websocket.send(stomp_frame(
                    "SUBSCRIBE",
                    {
                        "id": COMMAND_SUBSCRIPTION_ID,
                        "destination": f"/topic/agent.command.{agent_id}",
                        "ack": "auto",
                    },
                ))

                # 노드 전용 secret 수신 채널 구독
                await websocket.send(stomp_frame(
                    "SUBSCRIBE",
                    {
                        "id": "agent-secret-channel",
                        "destination": f"/topic/agent.secret.{agent_id}",
                        "ack": "auto",
                    },
                ))

                # 시스템 정보 수집 요청 채널 구독
                await websocket.send(stomp_frame(
                    "SUBSCRIBE",
                    {
                        "id": SYSINFO_SUBSCRIPTION_ID,
                        "destination": f"/topic/agent.sysinfo-request.{agent_id}",
                        "ack": "auto",
                    },
                ))
                print("[에이전트] 시스템 정보 요청 채널 구독 시작")

                if not agent_secret:
                    # 등록 직후 서버가 발급한 agent-secret을 받을 준비가 끝났음을 알립니다.
                    await websocket.send(stomp_frame(
                        "SEND",
                        {"destination": "/app/agent.register-ready", "content-type": "application/json"},
                        json.dumps({"nodeName": hostname, "agentId": agent_id}),
                    ))

                async def report_update_result(stage: str, success: bool | None = None, message: str = ""):
                    """업데이트 명령 ACK와 재연결 후 최신 커밋 확인 결과를 서버에 보고합니다."""
                    agent_dir = os.path.dirname(os.path.abspath(__file__))
                    current_sha, latest_sha, error = get_git_revisions(agent_dir)
                    resolved_success = (not error and current_sha == latest_sha) if success is None else success
                    await websocket.send(stomp_frame(
                        "SEND",
                        {"destination": "/app/agent.update-result", "content-type": "application/json"},
                        json.dumps({
                            "nodeName": hostname,
                            "agentId": agent_id,
                            "stage": stage,
                            "success": resolved_success,
                            "currentSha": current_sha,
                            "latestSha": latest_sha,
                            "message": message or error,
                        }),
                    ))

                async def report_update_available(current_sha: str, latest_sha: str) -> None:
                    await websocket.send(stomp_frame(
                        "SEND",
                        {"destination": "/app/agent.update-available", "content-type": "application/json"},
                        json.dumps({
                            "nodeName": hostname,
                            "agentId": agent_id,
                            "currentSha": current_sha,
                            "latestSha": latest_sha,
                        }),
                    ))

                async def check_and_apply_update(reason: str) -> None:
                    """GitHub 최신 커밋을 확인하고, 새 버전이 있으면 직접 업데이트 후 재시작합니다."""
                    async with update_lock:
                        agent_dir = os.path.dirname(os.path.abspath(__file__))
                        current_sha, latest_sha, error = get_git_revisions(agent_dir)
                        if error:
                            print(f"[에이전트] 업데이트 확인 오류: {error}")
                            await report_update_result("check-failed", False, error[-400:])
                            return

                        if latest_sha == current_sha:
                            await report_update_result("checked", True, f"{reason}: 최신 상태")
                            return

                        print(f"[에이전트] 업데이트 감지({reason}): {current_sha} → {latest_sha}")
                        await report_update_available(current_sha, latest_sha)
                        await report_update_result("started", True, f"{reason}: 업데이트 시작")

                        update_success, update_message = await platform_adapter.self_update(agent_dir)
                        if not update_success:
                            await report_update_result("failed", False, update_message[-400:])
                            print(f"[에이전트] 업데이트 실패: {update_message}")
                            return

                        await report_update_result("pulled", True, (update_message or "업데이트 적용 후 재시작")[-400:])
                        print("[에이전트] 업데이트 적용 완료; 재시작합니다.")
                        raise SystemExit(0)

                async def send_monitoring_loop():
                    """시스템 메트릭을 2초 간격으로 전송합니다."""
                    while True:
                        data = platform_adapter.collect_metrics()
                        await websocket.send(stomp_frame(
                            "SEND",
                            {"destination": "/app/monitoring", "content-type": "application/json"},
                            json.dumps(data),
                        ))
                        await asyncio.sleep(2)

                async def send_process_loop():
                    """프로세스 목록을 2초 간격으로 전송합니다."""
                    while True:
                        data = platform_adapter.list_processes()
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
                            svc_list = platform_adapter.list_services()
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
                        for session_id, queue in platform_adapter.iter_terminal_queues():
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

                async def check_update_loop():
                    """시작 시 1회, 이후 10분마다 GitHub 최신 커밋을 확인하고 필요하면 업데이트합니다."""
                    await check_and_apply_update("startup")
                    while True:
                        await asyncio.sleep(UPDATE_CHECK_INTERVAL_SECONDS)
                        await check_and_apply_update("scheduled")

                async def receive_commands_loop():
                    """백엔드에서 오는 명령(kill·터미널·시스템 정보·서비스 제어)을 수신하고 처리합니다."""
                    nonlocal agent_secret
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
                        if destination == f"/topic/agent.sysinfo-request.{agent_id}":
                            if payload.get("nodeName") == hostname:
                                try:
                                    loop = asyncio.get_event_loop()
                                    info = await loop.run_in_executor(None, platform_adapter.collect_hardware)
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

                        if cmd_type == "agent-secret":
                            if payload.get("nodeName") == hostname and payload.get("agentId") == agent_id:
                                new_secret = str(payload.get("agentSecret", "")).strip()
                                if new_secret:
                                    # 서버가 발급한 노드 전용 secret을 .env에 저장해 다음 재접속부터 account-token을 쓰지 않습니다.
                                    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
                                    lines = []
                                    found = False
                                    if os.path.exists(env_path):
                                        with open(env_path, "r", encoding="utf-8") as fh:
                                            for line in fh.read().splitlines():
                                                if line.startswith("AGENT_SECRET="):
                                                    lines.append(f"AGENT_SECRET={new_secret}")
                                                    found = True
                                                else:
                                                    lines.append(line)
                                    if not found:
                                        lines.append(f"AGENT_SECRET={new_secret}")
                                    with open(env_path, "w", encoding="utf-8") as fh:
                                        fh.write("\n".join(lines) + "\n")
                                    agent_secret = new_secret
                                    print("[agent] agent secret saved")
                            continue

                        # ── 서비스 제어 명령 ──
                        if cmd_type == "service-control":
                            if payload.get("nodeName") != hostname:
                                continue
                            svc_name = payload.get("name", "")
                            action = payload.get("action", "")
                            try:
                                message = platform_adapter.control_service(svc_name, action)
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

                        # ── 파일 목록 요청 처리 ──
                        if cmd_type == "file-list":
                            if payload.get("nodeName") != hostname:
                                continue
                            try:
                                response = platform_adapter.list_files(str(payload.get("path", "") or ""))
                            except Exception as e:
                                response = {
                                    "path": str(payload.get("path", "") or ""),
                                    "parent": "",
                                    "entries": [],
                                    "error": str(e),
                                }

                            await websocket.send(stomp_frame(
                                "SEND",
                                {"destination": "/app/file-list.result", "content-type": "application/json"},
                                json.dumps(response),
                            ))
                            continue

                        # ── 업데이트 명령 처리 ──
                        if cmd_type in ("update", "update-check"):
                            target_agent_id = str(payload.get("agentId", "") or "").strip()
                            target_node_name = str(payload.get("nodeName", "") or "").strip()
                            if target_agent_id:
                                if target_agent_id != agent_id:
                                    continue
                            elif target_node_name != hostname:
                                continue

                            print("[agent] update check command received")
                            try:
                                await check_and_apply_update("manual")
                            except SystemExit:
                                raise
                            except Exception as e:
                                update_message = str(e)
                                try:
                                    await report_update_result("failed", False, update_message[-400:])
                                except Exception as report_error:
                                    print(f"[agent] update failure report failed: {report_error}")
                                print(f"[agent] update failed: {update_message}")
                                continue
                            continue

                        # Uninstall command handling
                        if cmd_type == "uninstall":
                            if payload.get("nodeName") == hostname:
                                print("[agent] uninstall command received; sending ack")
                                # Send ACK first so the server can remove the node from the UI only after the agent receives the command.
                                await websocket.send(stomp_frame(
                                    "SEND",
                                    {"destination": "/app/agent.uninstall-ack", "content-type": "application/json"},
                                    json.dumps({
                                        "nodeName": hostname,
                                        "serviceName": service_name,
                                        "stage": "started",
                                    }),
                                ))
                                print("[agent] uninstall ack sent; starting self-removal")
                                agent_dir = os.path.dirname(os.path.abspath(__file__))
                                platform_adapter.start_self_uninstall(agent_dir, service_name)
                                raise SystemExit(0)
                            continue

                        # Terminal command handling
                        if cmd_type.startswith("terminal-"):
                            _handle_terminal_command(payload, cmd_type, hostname, platform_adapter)
                            continue

                        # ── kill 명령 처리 ──
                        if payload.get("nodeName") != hostname:
                            continue

                        pid = int(payload.get("pid", 0))
                        request_id = str(payload.get("requestId", "")).strip()
                        if not request_id or pid <= 0:
                            continue

                        try:
                            message = platform_adapter.kill_process(pid)
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

                        fresh = platform_adapter.list_processes()
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
                    check_update_loop(),
                )

        except Exception as e:
            print(f"[에이전트] 연결 에러 (5초 후 재시도): {e}")
            platform_adapter.cleanup_terminals()
            await asyncio.sleep(5)


def _handle_terminal_command(payload: dict, cmd_type: str, hostname: str, platform_adapter) -> None:
    """터미널 관련 명령을 분기 처리합니다."""
    if payload.get("nodeName") and payload.get("nodeName") != hostname:
        return

    session_id = payload.get("sessionId", "")

    if cmd_type == "terminal-open":
        platform_adapter.open_terminal(session_id, payload.get("cols", 80), payload.get("rows", 24))
    elif cmd_type == "terminal-input":
        platform_adapter.write_terminal(session_id, payload.get("data", ""))
    elif cmd_type == "terminal-resize":
        platform_adapter.resize_terminal(session_id, payload.get("cols", 80), payload.get("rows", 24))
    elif cmd_type == "terminal-close":
        platform_adapter.close_terminal(session_id)

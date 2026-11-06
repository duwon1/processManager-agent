"""Process Manager Agent 진입점입니다.
기본 실행은 WebSocket 에이전트 루프만 기동합니다.
AGENT_DEBUG_HTTP=1일 때만 127.0.0.1에 읽기 전용 디버그 HTTP API(FastAPI)를 함께 띄웁니다.
"""
import asyncio
import os
import signal
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from agent import run_agent
from config import get_settings
from pm_agent.platforms.base import PlatformAdapter
from pm_agent.platforms.factory import get_platform_adapter

settings = get_settings()
_platform_adapter: PlatformAdapter | None = None
# 디버그 HTTP API는 외부에 노출하지 않도록 loopback에만 바인딩합니다.
DEBUG_HTTP_HOST = "127.0.0.1"


def debug_http_enabled() -> bool:
    """AGENT_DEBUG_HTTP가 명시적으로 켜져 있을 때만 디버그 HTTP API를 기동합니다."""
    return os.getenv("AGENT_DEBUG_HTTP", "").strip().lower() in ("1", "true", "yes")


def get_adapter() -> PlatformAdapter:
    """현재 설정의 OS에 맞는 adapter를 지연 생성합니다."""
    global _platform_adapter
    if _platform_adapter is None:
        _platform_adapter = get_platform_adapter(settings.os_type)
    return _platform_adapter


def start_agent_task() -> asyncio.Task:
    """기동 정보를 출력하고 WebSocket 에이전트 태스크를 시작합니다."""
    print("에이전트 기동이 완료되었습니다.")
    print(f"WebSocket 대상 서버: {settings.websocket_url}")
    print(f"에이전트 호스트명: {settings.hostname}")
    print(f"에이전트 ID: {settings.agent_id}")

    return asyncio.create_task(
        run_agent(
            settings.websocket_url,
            settings.account_token,
            settings.hostname,
            settings.os_type,
            settings.agent_id,
            settings.service_name,
            settings.agent_secret,
        )
    )


async def stop_agent_task(agent_task: asyncio.Task) -> None:
    """터미널 세션을 정리하고 에이전트 태스크를 종료합니다."""
    print("에이전트 종료를 시작합니다.")
    get_adapter().cleanup_terminals()
    agent_task.cancel()
    try:
        await agent_task
    except asyncio.CancelledError:
        pass


async def run_without_http() -> None:
    """HTTP 서버 없이 에이전트 루프만 실행합니다. SIGTERM/SIGINT 시 정리 후 종료합니다."""
    agent_task = start_agent_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, agent_task.cancel)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows 이벤트 루프는 signal handler를 지원하지 않아 KeyboardInterrupt로 종료합니다.
            pass
    try:
        await agent_task
    except asyncio.CancelledError:
        pass
    finally:
        await stop_agent_task(agent_task)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 생명주기에 맞춰 에이전트 태스크를 시작하고 종료합니다."""
    agent_task = start_agent_task()
    yield
    await stop_agent_task(agent_task)


app = FastAPI(lifespan=lifespan)


@app.get("/monitoring")
def get_http_monitoring():
    """현재 시스템 메트릭을 HTTP로 즉시 조회합니다. 디버깅용으로 사용합니다."""
    return get_adapter().collect_metrics()


@app.get("/process/all")
def get_all_processes_http():
    """현재 프로세스 목록을 HTTP로 즉시 조회합니다. 디버깅용으로 사용합니다."""
    return get_adapter().list_processes()


if __name__ == "__main__":
    if debug_http_enabled():
        print(f"디버그 HTTP API 활성화: http://{DEBUG_HTTP_HOST}:{settings.port}")
        uvicorn.run("main:app", host=DEBUG_HTTP_HOST, port=settings.port, reload=settings.reload)
    else:
        try:
            asyncio.run(run_without_http())
        except KeyboardInterrupt:
            pass

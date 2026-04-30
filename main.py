"""Process Manager Agent 진입점입니다.
FastAPI 앱을 초기화하고 에이전트 태스크를 생명주기에 맞춰 관리합니다.
"""
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from agent import run_agent
from config import get_settings
from pm_agent.platforms.base import PlatformAdapter
from pm_agent.platforms.factory import get_platform_adapter

settings = get_settings()
_platform_adapter: PlatformAdapter | None = None


def get_adapter() -> PlatformAdapter:
    """현재 설정의 OS에 맞는 adapter를 지연 생성합니다."""
    global _platform_adapter
    if _platform_adapter is None:
        _platform_adapter = get_platform_adapter(settings.os_type)
    return _platform_adapter


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 생명주기에 맞춰 에이전트 태스크를 시작하고 종료합니다."""
    print("에이전트 기동이 완료되었습니다.")
    print(f"WebSocket 대상 서버: {settings.websocket_url}")
    print(f"에이전트 호스트명: {settings.hostname}")
    print(f"에이전트 ID: {settings.agent_id}")

    agent_task = asyncio.create_task(
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
    yield

    print("에이전트 종료를 시작합니다.")
    get_adapter().cleanup_terminals()
    agent_task.cancel()
    try:
        await agent_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)


@app.get("/monitoring")
def get_http_monitoring():
    """현재 시스템 메트릭을 HTTP로 즉시 조회합니다. 디버깅용으로 사용합니다."""
    return get_adapter().collect_metrics()


@app.get("/process/all")
def get_all_processes_http():
    """현재 프로세스 목록을 HTTP로 즉시 조회합니다. 디버깅용으로 사용합니다."""
    return get_adapter().list_processes()


@app.delete("/process/{pid}")
def kill_process_http(pid: int):
    """현재 OS adapter를 통해 프로세스를 종료합니다. 디버깅용으로 사용합니다."""
    return {"message": get_adapter().kill_process(pid)}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=settings.reload)

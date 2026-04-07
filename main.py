"""Process Manager Agent 진입점입니다.
FastAPI 앱을 초기화하고 에이전트 태스크를 생명주기에 맞춰 관리합니다.
"""
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from agent import run_agent
from terminal import terminal_manager
from system import metrics, process
from config import get_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 생명주기에 맞춰 에이전트 태스크를 시작하고 종료합니다."""
    print("에이전트 기동이 완료되었습니다.")
    print(f"WebSocket 대상 서버: {settings.websocket_url}")
    print(f"에이전트 호스트명: {settings.hostname}")

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
app.include_router(metrics.router)


@app.get("/monitoring")
def get_http_monitoring():
    """현재 시스템 메트릭을 HTTP로 즉시 조회합니다. 디버깅용으로 사용합니다."""
    return metrics.collect_system_metrics()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=settings.reload)

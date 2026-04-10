import os
import socket
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE  = BASE_DIR / ".env"


def load_env_file() -> None:
    """프로젝트 루트의 .env 파일을 읽어 환경변수로 등록합니다.
    이미 설정된 환경변수는 덮어쓰지 않습니다.
    """
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    """에이전트 실행에 필요한 불변 설정값을 담는 데이터 클래스입니다."""
    websocket_url: str
    account_token: str
    hostname: str
    os_type: str
    port: int
    reload: bool
    agent_id: str  # 에이전트 고유 UUID (재설치 시 동일 노드 식별)


def get_settings() -> Settings:
    """환경변수와 기본값을 합쳐 Settings 인스턴스를 반환합니다.
    ACCOUNT_TOKEN, SPRING_WS_URL이 없으면 즉시 RuntimeError를 발생시킵니다.
    """
    load_env_file()

    # SPRING_WS_URL은 .env에서 반드시 주입해야 합니다.
    websocket_url = os.getenv("SPRING_WS_URL", "").strip()
    if not websocket_url:
        raise RuntimeError("SPRING_WS_URL이 없습니다. .env 파일에 설정해주세요.")

    # ACCOUNT_TOKEN은 설치 시 반드시 주입해야 합니다.
    account_token = os.getenv("ACCOUNT_TOKEN", "").strip()
    if not account_token:
        raise RuntimeError("ACCOUNT_TOKEN이 없습니다. 설치 시 토큰을 주입해주세요.")

    hostname       = os.getenv("HOSTNAME", socket.gethostname() or "Linux-Server")
    os_type        = os.getenv("OS_TYPE", "Linux")
    port           = int(os.getenv("AGENT_PORT", "8888"))
    reload_enabled = os.getenv("LINUX_API_RELOAD", "false").lower() == "true"
    agent_id       = os.getenv("AGENT_ID", "").strip()

    return Settings(
        websocket_url=websocket_url,
        account_token=account_token,
        hostname=hostname,
        os_type=os_type,
        port=port,
        reload=reload_enabled,
        agent_id=agent_id,
    )

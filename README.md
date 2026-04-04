# Process Manager Agent

[Process Manager](https://github.com/duwon1/processManager) 백엔드와 연결되는 Python 에이전트입니다.
원격 서버에 설치하면 브라우저에서 실시간 모니터링, 프로세스 관리, 웹 터미널을 사용할 수 있습니다.

## 주요 기능

- **시스템 모니터링** - CPU, GPU, 메모리, 디스크, 네트워크 사용률을 2초 간격으로 전송
- **프로세스 관리** - 프로세스 목록 조회 및 원격 종료(kill)
- **웹 터미널** - PTY 기반 쉘 세션 (브라우저에서 SSH처럼 사용)
- **자동 재연결** - 백엔드 연결이 끊기면 5초 후 자동 재연결

## 기술 스택

- Python 3.10+
- FastAPI + Uvicorn
- psutil (시스템 메트릭)
- websockets (STOMP over WebSocket)
- PTY (가상 터미널)

## 설치 및 실행

```bash
# 클론
git clone https://github.com/duwon1/processManager-agent.git
cd processManager-agent

# 가상환경 생성
python3 -m venv .venv
source .venv/bin/activate

# 의존성 설치
pip install -r requirements.txt

# .env 파일 생성
cat > .env << EOF
ACCOUNT_TOKEN=your_account_token
SPRING_WS_URL=wss://your-backend-url/ws-native
HOSTNAME=your-server-name
OS_TYPE=Linux
AGENT_PORT=8888
EOF

# 실행
python main.py
```

## 환경변수

| 변수 | 필수 | 설명 | 기본값 |
|------|------|------|--------|
| ACCOUNT_TOKEN | O | 백엔드 인증 토큰 (메인 페이지에서 발급) | - |
| SPRING_WS_URL | X | 백엔드 WebSocket URL | wss://...ngrok.../ws-native |
| HOSTNAME | X | 서버 식별 이름 | 시스템 호스트명 |
| OS_TYPE | X | OS 종류 | Linux |
| AGENT_PORT | X | HTTP API 포트 | 8888 |

## 동작 원리

```
에이전트 → (아웃바운드 WebSocket) → 백엔드 (Spring Boot)
                                        ↕
                                    브라우저 (React)
```

에이전트가 백엔드에 먼저 연결하므로 포트포워딩이나 공인 IP 없이도 작동합니다.

## 관련 저장소

- [processManager](https://github.com/duwon1/processManager) - 메인 프로젝트 (백엔드 + 프론트엔드)

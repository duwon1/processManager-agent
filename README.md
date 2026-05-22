# Process Manager Agent

[Process Manager](https://github.com/duwon1/processManager) 백엔드와 연결되는 Python 에이전트입니다.
원격 서버에 설치하면 브라우저에서 실시간 모니터링, 프로세스 관리, 웹 터미널을 사용할 수 있습니다.

## 주요 기능

- **시스템 모니터링** - CPU, GPU, 메모리, 디스크, 네트워크 사용률을 2초 간격으로 전송
- **프로세스 관리** - 프로세스 목록 조회 및 원격 종료(kill), 읽기/쓰기 속도(MB/s) 실시간 표시
- **웹 터미널** - PTY 기반 쉘 세션 (브라우저에서 SSH처럼 사용)
- **자동 업데이트** - 새 버전 감지 시 대시보드에서 원클릭 업데이트
- **자동 재연결** - 백엔드 연결이 끊기면 5초 후 자동 재연결

## 기술 스택

- Python 3.10+
- FastAPI + Uvicorn
- psutil (시스템 메트릭)
- websockets (STOMP over WebSocket)
- PTY (가상 터미널)

## 설치



설치 중 노드 이름 입력 프롬프트가 표시됩니다. 엔터를 누르면 시스템 호스트명이 자동으로 사용됩니다.

## 환경변수

| 변수 | 필수 | 설명 | 기본값 |
|------|------|------|--------|
| ACCOUNT_TOKEN | △ | 최초 설치/재설치에 사용하는 1회용 설치 토큰 | - |
| AGENT_SECRET | △ | 등록 후 재접속에 사용하는 노드 전용 secret | - |
| SPRING_WS_URL | O | 백엔드 WebSocket URL | - |
| HOSTNAME | X | 서버 식별 이름 | 시스템 호스트명 |
| OS_TYPE | X | OS 종류 (`Linux`, `Windows`) | 실행 OS |
| AGENT_PORT | X | HTTP API 포트 | 8888 |
| LINUX_API_RELOAD | X | FastAPI 개발 모드 리로드 | false |

`ACCOUNT_TOKEN`은 최초 등록이 끝나면 비워지고, 이후 재접속은 서버가 발급한 `AGENT_SECRET`으로 처리됩니다.

현재 Windows/Linux 에이전트는 실시간 모니터링 메트릭, 프로세스 조회/종료, 서비스 조회/제어, 하드웨어 상세 조회, 장치 관리자형 인벤토리, 자동 업데이트를 지원합니다. 터미널은 Windows/Linux 모두 지원하며, 파일 조회는 Linux 전용입니다.

## 동작 원리



에이전트가 백엔드에 먼저 연결하므로 포트포워딩이나 공인 IP 없이도 작동합니다.

## 관련 저장소

- [processManager](https://github.com/duwon1/processManager) - 메인 프로젝트 (백엔드 + 프론트엔드)

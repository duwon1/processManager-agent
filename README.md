# Process Manager Agent

Process Manager Agent는 [Process Manager](https://github.com/duwon1/processManager)에 연결되는 원격 노드 프로그램입니다.
관리 대상 PC/서버에서 실행되며, 시스템 상태 수집과 프로세스/서비스/터미널 제어 요청을 처리합니다.

## 역할

이 저장소는 웹 화면을 제공하지 않습니다.
메인 웹 서비스와 WebSocket으로 연결되어 원격 장비의 데이터를 수집하고, 사용자가 웹에서 요청한 제어 명령을 실제 장비에서 실행합니다.

```text
Process Manager Web
        |
        | REST / STOMP WebSocket
        v
Process Manager Backend
        |
        | STOMP WebSocket
        v
Process Manager Agent
        |
        | psutil / systemctl / PowerShell / PTY
        v
Managed PC or Server
```

에이전트가 백엔드로 먼저 아웃바운드 연결을 만들기 때문에, 관리 대상 장비에 공인 IP나 포트포워딩을 설정하지 않아도 사용할 수 있습니다.

## 주요 기능

| 기능 | 설명 |
|------|------|
| 실시간 모니터링 | CPU, GPU, 메모리, 디스크, 네트워크 사용률 수집 |
| 프로세스 관리 | 프로세스 목록 조회, CPU/메모리/I/O 사용량 전송, 프로세스 종료 |
| 서비스 관리 | 서비스 목록 조회, 실행, 중지, 재시작 |
| 웹 터미널 | 브라우저 터미널 입력을 실제 장비의 PowerShell/CMD/Linux shell로 전달 |
| 하드웨어 정보 | CPU, 메모리, 디스크, GPU, 네트워크 어댑터 등 장비 정보 수집 |
| 장치 인벤토리 | 장치 관리자 형태의 장치/드라이버 정보 수집 |
| 자동 업데이트 | 서버가 전달한 목표 커밋으로 업데이트하고 해시 잠금 의존성을 설치 |
| 자가 삭제 | 웹에서 삭제 명령을 받으면 서비스/작업 등록과 로컬 파일 정리 |
| 자동 재연결 | 연결 실패 시 재시도하고, 반복 실패 시 watchdog 재시작 유도 |

## 지원 범위

| 기능 | Windows | Linux |
|------|---------|-------|
| 시스템 모니터링 | 지원 | 지원 |
| 프로세스 조회/종료 | 지원 | 지원 |
| 서비스 조회/제어 | 지원 | 지원 |
| 터미널 | PowerShell/CMD | Shell/PTY |
| 파일 목록 조회 | 미지원 | 지원 |
| 하드웨어 상세 | 지원 | 지원 |
| 장치 인벤토리 | 지원 | 지원 |
| 자동 업데이트 | 지원 | 지원 |
| 자가 삭제 | 지원 | 지원 |

## 프로젝트 구조

```text
processManager-agent
├─ main.py                  # FastAPI 진입점, 에이전트 생명주기 관리
├─ agent.py                 # STOMP WebSocket 연결, 데이터 전송, 명령 처리
├─ config.py                # .env 로딩과 실행 설정
├─ stomp.py                 # STOMP 프레임 생성/파싱
├─ terminal.py              # Linux PTY 터미널 구현
├─ pm_agent/platforms/      # OS별 기능 adapter
│  ├─ base.py               # 공통 인터페이스
│  ├─ factory.py            # OS별 adapter 선택
│  ├─ windows/              # Windows 메트릭, 서비스, 터미널, 업데이트
│  └─ linux/                # Linux 메트릭, 서비스, 파일, 터미널, 업데이트
├─ requirements.txt         # 기본 Python 의존성
└─ requirements.lock        # 해시 잠금 의존성
```

## 실행 흐름

1. `.env`에서 백엔드 WebSocket 주소와 등록 토큰 또는 노드 secret을 읽습니다.
2. 현재 OS에 맞는 adapter를 선택합니다.
3. 백엔드에 STOMP WebSocket으로 연결합니다.
4. 모니터링, 프로세스, 서비스 정보를 주기적으로 전송합니다.
5. 백엔드에서 수신한 프로세스 종료, 서비스 제어, 터미널 입력, 업데이트 명령을 처리합니다.
6. 연결이 끊기면 터미널 세션을 정리하고 재연결을 시도합니다.

## 설치 및 실행

일반 설치는 메인 웹 서비스의 프로필 화면에서 1회용 설치 토큰을 발급받은 뒤, 화면에 표시되는 설치 명령어를 관리 대상 장비에서 실행합니다.
수동으로 실행할 때는 `.env` 파일을 만든 뒤 Python 의존성을 설치하고 `main.py`를 실행합니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Windows PowerShell에서는 다음처럼 가상환경을 활성화할 수 있습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

설치 중 노드 이름 입력 프롬프트가 표시되면 원하는 이름을 입력합니다.
엔터를 누르면 시스템 호스트명이 자동으로 사용됩니다.

## 환경변수

| 변수 | 필수 | 설명 | 기본값 |
|------|------|------|--------|
| `SPRING_WS_URL` | O | 백엔드 STOMP WebSocket 주소 | - |
| `ACCOUNT_TOKEN` | △ | 최초 등록/재설치에 사용하는 1회용 설치 토큰 | - |
| `AGENT_SECRET` | △ | 등록 후 재접속에 사용하는 노드 전용 secret | - |
| `AGENT_ID` | X | 서버가 발급한 노드 고유 ID | - |
| `HOSTNAME` | X | 웹에 표시할 노드 이름 | 시스템 호스트명 |
| `OS_TYPE` | X | 강제로 지정할 OS 타입 (`Windows`, `Linux`) | 실행 OS |
| `AGENT_PORT` | X | FastAPI HTTP API 포트 | `8888` |
| `LINUX_API_RELOAD` | X | FastAPI 개발 모드 리로드 | `false` |
| `INSTANCE` | X | 설치 인스턴스 구분값 | `default` |
| `SERVICE_NAME` | X | 업데이트/삭제 시 제어할 서비스 또는 작업 이름 | `processmanager-agent` |
| `TERMINAL_SHELL` | X | 기본 터미널 shell 경로 또는 명령 | OS 기본값 |
| `TERMINAL_USER` | X | Linux 터미널 실행 사용자 | 현재 사용자 |

`ACCOUNT_TOKEN`은 최초 등록에 사용됩니다.
등록이 완료되면 서버가 `AGENT_SECRET`을 발급하고, 이후 재접속은 `AGENT_SECRET`으로 처리됩니다.

## 로컬 확인용 HTTP API

FastAPI 서버는 디버깅용 HTTP API를 제공합니다.

| Method | Path | 설명 |
|--------|------|------|
| `GET` | `/monitoring` | 현재 시스템 메트릭 즉시 조회 |
| `GET` | `/process/all` | 현재 프로세스 목록 조회 |
| `DELETE` | `/process/{pid}` | 지정 PID 프로세스 종료 |

운영 기능은 백엔드와의 STOMP WebSocket 연결을 통해 동작하며, 위 HTTP API는 로컬 확인용입니다.

## 업데이트 방식

에이전트는 주기적으로 원격 Git 커밋을 확인하고, 새 커밋이 있으면 백엔드에 업데이트 가능 상태를 보고합니다.
서버가 수동 업데이트 명령과 함께 `targetSha`를 전달하면 해당 커밋으로 checkout한 뒤 재시작합니다.

업데이트 중 `requirements.lock`이 있으면 `pip --require-hashes`로 의존성을 설치합니다.
이를 통해 업데이트 시점의 Python 패키지 버전과 해시를 고정합니다.

Windows는 예약 작업 기반 재시작 흐름을 사용하고, Linux는 systemd 서비스 기준으로 업데이트/재시작 흐름을 처리합니다.

## 관련 저장소

- [processManager](https://github.com/duwon1/processManager) - 백엔드와 프론트엔드를 포함한 메인 프로젝트

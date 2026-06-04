"""systemd 서비스 목록 수집 및 제어."""
import re
import shutil
import subprocess


SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+(?:\.service)?$")
SUDO_DENIED_TERMS = (
    "a password is required",
    "a terminal is required",
    "not allowed to execute",
    "may not run sudo",
    "password is required",
)


def get_service_list() -> list:
    """systemctl로 서비스 목록을 수집합니다. (로드된 서비스만)"""
    try:
        output = subprocess.check_output(
            ['systemctl', 'list-units', '--type=service', '--all', '--plain', '--no-legend'],
            text=True, timeout=5
        )
        services = []
        for line in output.strip().splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            load_state = parts[1]
            # not-found 서비스는 제외합니다.
            if load_state == 'not-found':
                continue
            services.append({
                'name':        parts[0],
                'loadState':   load_state,
                'activeState': parts[2],
                'subState':    parts[3],
                'description': parts[4].strip() if len(parts) > 4 else '',
            })
        return services
    except Exception as e:
        print(f"[서비스] 목록 수집 오류: {e}")
        return []


def control_service(name: str, action: str) -> str:
    """서비스를 제어합니다. action: start | stop | restart"""
    allowed = {'start', 'stop', 'restart'}
    if action not in allowed:
        raise ValueError(f"허용되지 않은 액션: {action}")
    name = (name or "").strip()
    if not SERVICE_NAME_RE.fullmatch(name):
        raise ValueError(f"허용되지 않은 서비스 이름: {name}")
    if not name.endswith('.service'):
        name = f"{name}.service"
    systemctl_bin = shutil.which("systemctl") or "/usr/bin/systemctl"
    try:
        subprocess.run(
            ['sudo', '-n', systemctl_bin, action, name],
            check=True, timeout=15,
            capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        # systemctl이 반환한 실제 오류 메시지를 전달합니다.
        detail = (e.stderr or e.stdout or str(e)).strip()
        if _is_sudo_denied(detail):
            detail = "서비스 제어 sudoers 권한이 없습니다. 리눅스 에이전트 설치 명령어를 다시 실행해 권한을 갱신하세요."
        raise RuntimeError(detail) from e
    return f"{name} {action} 완료"


def _is_sudo_denied(detail: str) -> bool:
    lower_detail = (detail or "").lower()
    return any(term in lower_detail for term in SUDO_DENIED_TERMS)

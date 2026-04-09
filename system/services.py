"""systemd 서비스 목록 수집 및 제어."""
import subprocess


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
    """서비스를 제어합니다. action: start | stop | restart | enable | disable"""
    allowed = {'start', 'stop', 'restart', 'enable', 'disable'}
    if action not in allowed:
        raise ValueError(f"허용되지 않은 액션: {action}")
    if not name.endswith('.service'):
        name = f"{name}.service"
    try:
        subprocess.run(
            ['sudo', 'systemctl', action, name],
            check=True, timeout=15,
            capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        # systemctl이 반환한 실제 오류 메시지를 전달합니다.
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise RuntimeError(detail) from e
    return f"{name} {action} 완료"

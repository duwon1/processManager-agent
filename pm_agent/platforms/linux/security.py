"""Linux 에이전트 sudoers를 최소 권한 규칙으로 맞추는 helper입니다.

에이전트는 어떤 경우에도 `NOPASSWD: ALL` 규칙을 쓰지 않습니다.
설치 스크립트(install.sh)가 쓰는 limited-sudoers-v3 규칙과 같은 마커를 사용하며,
과거 버전이 남긴 전체 허용(full-sudoers-v1) 규칙만 에이전트가 실제로 쓰는 명령으로 좁힙니다.
프로세스 종료/서비스 제어는 와일드카드 규칙 대신 root 소유 래퍼(pm-kill/pm-service)만 허용합니다.
"""
from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from system.privileged import PM_KILL_PATH, PM_SERVICE_PATH, WRAPPER_DIR

try:
    import pwd
except ImportError:  # pragma: no cover - Linux runtime provides pwd.
    pwd = None


SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
USER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
ABSOLUTE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
BASE_SERVICE_NAME = "processmanager-agent"
BASE_SUDOERS_PATH = "/etc/sudoers.d/processmanager"
HARDENING_MARKER = ".sudoers_hardening_checked"
# install.sh와 같은 마커입니다. 설치 스크립트가 쓴 규칙은 그대로 두고 건드리지 않습니다.
HARDENING_MARKER_VERSION = "limited-sudoers-v3"
# 저장소에 포함된 래퍼 원본 경로(에이전트 디렉터리 기준)와 설치 경로입니다.
WRAPPER_SOURCES = {
    "scripts/linux/pm-kill": PM_KILL_PATH,
    "scripts/linux/pm-service": PM_SERVICE_PATH,
}


async def ensure_agent_sudoers(agent_dir: str, service_name: str) -> tuple[bool, str]:
    """에이전트 사용자의 sudoers를 에이전트가 실제로 쓰는 명령만 허용하는 규칙으로 맞춥니다.

    마커가 limited-sudoers-v3이면(설치 스크립트가 설정했거나 이미 좁혔으면) 아무것도 하지 않습니다.
    이 함수는 권한을 넓히지 않습니다. sudo 자체가 이미 제한돼 visudo/install을 실행할 수 없으면
    현재 상태를 유지하고 마커만 기록합니다.
    """
    if not SERVICE_NAME_RE.fullmatch(service_name or ""):
        return False, f"invalid service name: {service_name!r}"

    visudo_bin = shutil.which("visudo") or "/usr/sbin/visudo"
    install_bin = shutil.which("install") or "/usr/bin/install"
    agent_user = resolve_agent_user(agent_dir)
    if not USER_NAME_RE.fullmatch(agent_user):
        return False, f"invalid agent user: {agent_user!r}"

    sudoers_path = resolve_sudoers_path(service_name)
    marker_path = Path(agent_dir) / HARDENING_MARKER
    if marker_is_current(marker_path):
        return True, "sudoers setup skipped: already checked"

    terminal_user = os.getenv("TERMINAL_USER", "").strip()
    terminal_shell = os.getenv("TERMINAL_SHELL", "").strip() or os.getenv("SHELL", "").strip() or "/bin/bash"
    try:
        desired = build_agent_sudoers(agent_user, service_name, terminal_user, terminal_shell)
    except ValueError as exc:
        return False, str(exc)

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(desired)
            temp_path = tmp.name
        os.chmod(temp_path, 0o440)

        # sudoers를 쓰기 전에 래퍼를 먼저 설치합니다. 실패해도 에이전트는 구버전 직접 명령으로 대체합니다.
        install_wrappers(agent_dir, install_bin)

        validation = run_sudo([visudo_bin, "-cf", temp_path])
        if validation.returncode != 0:
            if sudo_denied(validation):
                write_marker(marker_path, "sudo already restricted")
                return True, "sudoers setup skipped: sudo is already restricted"
            return False, clean_output(validation) or "sudoers validation failed"

        installed = run_sudo([install_bin, "-m", "0440", "-o", "root", "-g", "root", temp_path, sudoers_path])
        if installed.returncode != 0:
            if sudo_denied(installed):
                write_marker(marker_path, "sudo already restricted")
                return True, "sudoers setup skipped: sudo is already restricted"
            return False, clean_output(installed) or "sudoers install failed"

        write_marker(marker_path, f"installed {sudoers_path}")
        return True, f"sudoers limited: {sudoers_path}"
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def resolve_agent_user(agent_dir: str) -> str:
    if pwd is not None:
        try:
            return pwd.getpwuid(Path(agent_dir).stat().st_uid).pw_name
        except Exception:
            pass
    return os.getenv("SUDO_USER") or getpass.getuser()


def resolve_sudoers_path(service_name: str) -> str:
    prefix = BASE_SERVICE_NAME + "-"
    if service_name.startswith(prefix):
        suffix = service_name[len(prefix):]
        if suffix and re.fullmatch(r"[A-Za-z0-9_-]+", suffix):
            return BASE_SUDOERS_PATH + "-" + suffix
    return BASE_SUDOERS_PATH


def build_agent_sudoers(
    agent_user: str,
    service_name: str,
    terminal_user: str = "",
    terminal_shell: str = "",
    systemctl_bin: str | None = None,
    rm_bin: str | None = None,
    kill_bin: str | None = None,
    dmidecode_bin: str | None = None,
) -> str:
    """에이전트 코드가 실제로 `sudo`로 실행하는 명령만 허용하는 sudoers 내용을 만듭니다.

    - 자기 서비스 관리(업데이트/삭제): install.sh와 같은 restart/stop/disable, daemon-reload, unit 파일 삭제
    - 프로세스 종료(system/process.py): root 소유 래퍼 pm-kill <pid> (인자는 래퍼가 검증)
    - 서비스 제어(system/services.py): root 소유 래퍼 pm-service <start|stop|restart> <name> (인자는 래퍼가 검증)
    - 메모리 하드웨어 정보(system/metrics.py, system/hardware.py): dmidecode -t 17, dmidecode -t memory
    - 터미널 사용자 전환(terminal.py): root가 아닌 TERMINAL_USER로 지정 shell --login 실행
    """
    if not USER_NAME_RE.fullmatch(agent_user or ""):
        raise ValueError(f"invalid agent user: {agent_user!r}")
    if not SERVICE_NAME_RE.fullmatch(service_name or ""):
        raise ValueError(f"invalid service name: {service_name!r}")
    systemctl_bin = systemctl_bin or shutil.which("systemctl") or "/usr/bin/systemctl"
    rm_bin = rm_bin or shutil.which("rm") or "/bin/rm"
    # kill_bin은 하위 호환용 인자입니다. v3부터 kill은 pm-kill 래퍼로만 허용합니다.
    dmidecode_bin = dmidecode_bin or shutil.which("dmidecode") or "/usr/sbin/dmidecode"
    for binary in (systemctl_bin, rm_bin, dmidecode_bin):
        if not ABSOLUTE_PATH_RE.fullmatch(binary):
            raise ValueError(f"invalid command path: {binary!r}")

    root_commands = [
        f"{systemctl_bin} restart {service_name}",
        f"{systemctl_bin} stop {service_name}",
        f"{systemctl_bin} disable {service_name}",
        f"{systemctl_bin} daemon-reload",
        f"{rm_bin} -f /etc/systemd/system/{service_name}.service",
        PM_KILL_PATH,
        PM_SERVICE_PATH,
        f"{dmidecode_bin} -t 17",
        f"{dmidecode_bin} -t memory",
    ]
    lines = [f"{agent_user} ALL=(root) NOPASSWD: " + ", ".join(root_commands)]

    # root 셸을 비밀번호 없이 열어주는 규칙은 만들지 않습니다.
    if (
        terminal_user
        and terminal_user not in ("root", agent_user)
        and USER_NAME_RE.fullmatch(terminal_user)
        and ABSOLUTE_PATH_RE.fullmatch(terminal_shell or "")
    ):
        lines.append(f"{agent_user} ALL=({terminal_user}) NOPASSWD: {terminal_shell} --login")
    return "\n".join(lines) + "\n"


def install_wrappers(agent_dir: str, install_bin: str) -> bool:
    """저장소의 래퍼 원본을 root:root 0755로 설치합니다. 이미 있으면 덮어쓰지 않습니다."""
    if all(os.path.isfile(target) for target in WRAPPER_SOURCES.values()):
        return True
    created = run_sudo([install_bin, "-d", "-m", "0755", "-o", "root", "-g", "root", WRAPPER_DIR])
    if created.returncode != 0:
        return False
    ok = True
    for relative, target in WRAPPER_SOURCES.items():
        source = Path(agent_dir) / relative
        if not source.is_file():
            ok = False
            continue
        installed = run_sudo([install_bin, "-m", "0755", "-o", "root", "-g", "root", str(source), target])
        ok = ok and installed.returncode == 0
    return ok


def marker_is_current(marker_path: Path) -> bool:
    try:
        first_line = marker_path.read_text(encoding="utf-8").splitlines()[0]
        return first_line == HARDENING_MARKER_VERSION
    except (OSError, IndexError):
        return False


def write_marker(marker_path: Path, message: str) -> None:
    try:
        marker_path.write_text(HARDENING_MARKER_VERSION + "\n" + message + "\n", encoding="utf-8")
    except OSError:
        pass


def run_sudo(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", *args],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def sudo_denied(result: subprocess.CompletedProcess[str]) -> bool:
    output = clean_output(result).lower()
    denied_terms = (
        "a password is required",
        "a terminal is required",
        "not allowed to execute",
        "may not run sudo",
        "password is required",
    )
    return any(term in output for term in denied_terms)


def clean_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()

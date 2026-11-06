"""프로세스 종료/서비스 제어 명령의 보호 대상 규칙입니다.

서버에서 온 명령이라도 에이전트 자신, 원격 접속 경로(sshd), init 계열과 OS 핵심 프로세스/서비스는
종료하거나 중지하지 않도록 adapter 호출 전에 한 번 더 확인합니다.
Linux에서는 root 소유 래퍼(pm-kill/pm-service)가 같은 규칙을 한 번 더 검사합니다.
"""
from __future__ import annotations

import os
from typing import Any

# 이름으로 보호하는 프로세스 (소문자, 확장자 제외 기준)
# - Linux: init 계열과 원격 접속 경로(sshd, OpenSSH 9.8+의 sshd-session)
# - Windows: 종료 시 OS가 멈추거나 재부팅되는 핵심 프로세스 (svchost는 범위가 너무 넓어 제외)
PROTECTED_PROCESS_NAMES = frozenset({
    "sshd", "sshd-session", "systemd", "init",
    "csrss", "wininit", "lsass", "smss", "services", "winlogon",
})
# 모든 액션(start 포함)을 거부하는 서비스 접두사와 unit 종류입니다.
# dbus-org.*는 systemd-logind 등의 D-Bus 별칭이고, debug-shell은 인증 없는 root 셸을 엽니다.
ALWAYS_PROTECTED_SERVICE_PREFIXES = ("systemd-", "dbus-org.")
ALWAYS_PROTECTED_SERVICE_NAMES = frozenset({"debug-shell"})
NON_SERVICE_UNIT_SUFFIXES = (
    ".target", ".socket", ".device", ".mount", ".automount", ".swap", ".timer", ".path", ".slice", ".scope",
)
# stop/restart를 거부하는 서비스 이름과 접두사 (소문자, .service 제외 기준)
# Windows 서비스 이름은 대소문자를 구분하지 않으므로 소문자로 비교합니다. (TermService, WinRM, RpcSs, EventLog)
PROTECTED_SERVICE_NAMES = frozenset({
    "sshd", "ssh", "dbus", "dbus-broker", "networkmanager", "networking", "systemd-networkd",
    "firewalld", "ufw", "nftables", "iptables", "ip6tables", "auditd",
    "termservice", "winrm", "rpcss", "eventlog",
})
# 템플릿 인스턴스(sshd@x, user@1000, getty@tty1 등)도 같은 이유로 보호합니다.
PROTECTED_SERVICE_PREFIXES = (
    "systemd-", "processmanager-agent-", "sshd@", "ssh@", "user@", "getty@", "serial-getty@",
)
PROTECTED_SERVICE_EXACT_AGENT = "processmanager-agent"
PROTECTED_SERVICE_ACTIONS = frozenset({"stop", "restart"})

PROCESS_CHANGED_MESSAGE = "프로세스가 바뀌어 종료하지 않았습니다"
UNKNOWN_PROCESS_MESSAGE = "프로세스 이름을 확인할 수 없어(권한 부족) 보호 대상 여부를 알 수 없으므로 종료하지 않았습니다."


def _normalize_process_name(name: str | None) -> str:
    normalized = (name or "").strip().lower()
    if normalized.endswith(".exe"):
        normalized = normalized[: -len(".exe")]
    return normalized


def _normalize_service_name(name: str | None) -> str:
    normalized = (name or "").strip().lower()
    if normalized.endswith(".service"):
        normalized = normalized[: -len(".service")]
    return normalized


def protected_pid_reason(pid: int, self_pid: int | None = None, parent_pid: int | None = None) -> str | None:
    """PID만으로 판단 가능한 보호 대상이면 거부 사유를, 아니면 None을 반환합니다."""
    self_pid = os.getpid() if self_pid is None else self_pid
    parent_pid = os.getppid() if parent_pid is None else parent_pid
    if pid == 1:
        return "PID 1(init/systemd)은 보호 대상이라 종료하지 않았습니다."
    if pid == self_pid:
        return "에이전트 자기 자신은 종료할 수 없습니다."
    if parent_pid and pid == parent_pid:
        return "에이전트의 부모 프로세스는 보호 대상이라 종료하지 않았습니다."
    # Linux에서는 스레드 ID(TID)를 kill하면 프로세스 전체가 죽어 위 비교를 우회할 수 있으므로 프로세스 ID만 허용합니다.
    tgid = _linux_tgid(pid)
    if tgid is not None and tgid != pid:
        return "스레드 ID가 아닌 프로세스 ID만 종료할 수 있습니다."
    return None


def _linux_tgid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as status:
            for line in status:
                if line.startswith("Tgid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def protected_process_name_reason(process_name: str | None) -> str | None:
    """보호 대상 프로세스 이름이면 거부 사유를, 아니면 None을 반환합니다."""
    normalized = _normalize_process_name(process_name)
    if normalized in PROTECTED_PROCESS_NAMES:
        return f"{normalized} 프로세스는 보호 대상이라 종료하지 않았습니다."
    return None


def protected_service_reason(name: str | None, action: str | None, own_service_name: str | None) -> str | None:
    """보호 대상 서비스 제어면 거부 사유를, 아니면 None을 반환합니다.

    - systemd-* 서비스와 .service가 아닌 unit(*.target 등)은 start를 포함한 모든 액션을 거부합니다.
    - 원격 접속/네트워크/메시지 버스/에이전트 서비스는 stop/restart를 거부합니다.
    """
    normalized_action = (action or "").strip().lower()
    raw = (name or "").strip().lower()
    if raw.endswith(NON_SERVICE_UNIT_SUFFIXES):
        return f"{name}은(는) 서비스(.service)가 아닌 unit이라 {normalized_action}하지 않았습니다."
    normalized = _normalize_service_name(name)
    if normalized.startswith(ALWAYS_PROTECTED_SERVICE_PREFIXES) or normalized in ALWAYS_PROTECTED_SERVICE_NAMES:
        return f"{name} 서비스는 보호 대상이라 {normalized_action}하지 않았습니다."
    if normalized_action not in PROTECTED_SERVICE_ACTIONS:
        return None
    own = _normalize_service_name(own_service_name)
    if own and normalized == own:
        return f"에이전트 자기 서비스({name})는 원격에서 {normalized_action}할 수 없습니다."
    if (
        normalized in PROTECTED_SERVICE_NAMES
        or normalized == PROTECTED_SERVICE_EXACT_AGENT
        or normalized.startswith(PROTECTED_SERVICE_PREFIXES)
    ):
        return f"{name} 서비스는 보호 대상이라 {normalized_action}하지 않았습니다."
    return None


def started_at_matches(expected: str, current: str | None) -> bool:
    """서버가 보낸 startedAt과 현재 started_at이 같은 포맷 문자열로 정확히 일치하는지 비교합니다."""
    return current is not None and current == expected


def execute_kill(platform_adapter: Any, pid: int, started_at: str | None = None) -> tuple[bool, str]:
    """보호 대상/프로세스 교체 여부를 확인한 뒤 adapter로 프로세스를 종료합니다.

    거부한 경우 (False, 사유)를 반환하고, adapter.kill_process 예외는 호출자에게 그대로 전달합니다.
    """
    reason = protected_pid_reason(pid)
    if reason:
        return False, reason

    expected_started_at = str(started_at or "").strip()
    try:
        process_name = platform_adapter.get_process_name(pid)
    except LookupError:
        if expected_started_at:
            return False, PROCESS_CHANGED_MESSAGE
        process_name = None
    if process_name is not None and not str(process_name).strip():
        # AccessDenied 등으로 이름을 읽지 못하면 보호 대상인지 알 수 없으므로 종료하지 않습니다.
        return False, UNKNOWN_PROCESS_MESSAGE
    reason = protected_process_name_reason(process_name)
    if reason:
        return False, reason

    if expected_started_at:
        # 프로세스 목록에 보고한 started_at과 같은 포맷 함수로 현재 값을 다시 계산해 비교합니다.
        try:
            current_started_at = platform_adapter.get_process_started_at(pid)
        except LookupError:
            current_started_at = None
        if not started_at_matches(expected_started_at, current_started_at):
            return False, PROCESS_CHANGED_MESSAGE

    return True, platform_adapter.kill_process(pid)

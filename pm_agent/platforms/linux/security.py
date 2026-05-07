"""Linux runtime security hardening helpers."""
from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    import pwd
except ImportError:  # pragma: no cover - Linux runtime provides pwd.
    pwd = None


SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
USER_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
BASE_SERVICE_NAME = "processmanager-agent"
BASE_SUDOERS_PATH = "/etc/sudoers.d/processmanager"


async def ensure_limited_sudoers(agent_dir: str, service_name: str) -> tuple[bool, str]:
    """Replace legacy NOPASSWD: ALL sudoers with the minimum agent commands.

    Existing agents may still have broad sudo from older installers. When they
    auto-update into this version, startup calls this helper once the new code
    is running. If sudo is already restricted, the write attempt can be denied;
    that is treated as non-fatal because the broad rule is no longer available.
    """
    if not SERVICE_NAME_RE.fullmatch(service_name or ""):
        return False, f"invalid service name: {service_name!r}"

    systemctl_bin = shutil.which("systemctl") or "/usr/bin/systemctl"
    rm_bin = shutil.which("rm") or "/usr/bin/rm"
    visudo_bin = shutil.which("visudo") or "/usr/sbin/visudo"
    install_bin = shutil.which("install") or "/usr/bin/install"
    agent_user = resolve_agent_user(agent_dir)
    if not USER_NAME_RE.fullmatch(agent_user):
        return False, f"invalid agent user: {agent_user!r}"

    sudoers_path = resolve_sudoers_path(service_name)
    desired = build_limited_sudoers(agent_user, service_name, systemctl_bin, rm_bin)

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(desired)
            temp_path = tmp.name
        os.chmod(temp_path, 0o440)

        validation = run_sudo([visudo_bin, "-cf", temp_path])
        if validation.returncode != 0:
            if sudo_denied(validation):
                return True, "sudoers hardening skipped: sudo is already restricted"
            return False, clean_output(validation) or "sudoers validation failed"

        installed = run_sudo([install_bin, "-m", "0440", "-o", "root", "-g", "root", temp_path, sudoers_path])
        if installed.returncode != 0:
            if sudo_denied(installed):
                return True, "sudoers hardening skipped: sudo is already restricted"
            return False, clean_output(installed) or "sudoers install failed"

        final_validation = run_sudo([visudo_bin, "-cf", sudoers_path])
        if final_validation.returncode != 0:
            return False, clean_output(final_validation) or "installed sudoers validation failed"

        return True, f"sudoers hardened: {sudoers_path}"
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


def build_limited_sudoers(agent_user: str, service_name: str, systemctl_bin: str, rm_bin: str) -> str:
    service_file = f"/etc/systemd/system/{service_name}.service"
    return "\n".join([
        f"{agent_user} ALL=(root) NOPASSWD: {systemctl_bin} restart {service_name}",
        f"{agent_user} ALL=(root) NOPASSWD: {systemctl_bin} stop {service_name}",
        f"{agent_user} ALL=(root) NOPASSWD: {systemctl_bin} disable {service_name}",
        f"{agent_user} ALL=(root) NOPASSWD: {systemctl_bin} daemon-reload",
        f"{agent_user} ALL=(root) NOPASSWD: {rm_bin} -f {service_file}",
        "",
    ])


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

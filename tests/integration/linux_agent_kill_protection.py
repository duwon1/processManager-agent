"""실제 systemd에서 다른 에이전트 서비스의 MainPID 종료가 거부되는지 확인합니다.

CI(Ubuntu 러너) 전용입니다. processmanager-agent-citest.service가 현재 사용자로 실행 중이어야 합니다.
같은 사용자라 보호가 없으면 os.kill로 바로 종료되는 경로입니다.
"""
import os
import subprocess
import sys
import time

from pm_agent import protection
from pm_agent.platforms.linux import processes
from pm_agent.platforms.linux.adapter import LinuxAdapter

UNIT = "processmanager-agent-citest.service"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


main_pid = int(subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", UNIT], text=True).strip())
assert main_pid > 0, "테스트용 에이전트 서비스가 실행 중이 아닙니다."
owner_uid = os.stat(f"/proc/{main_pid}").st_uid
assert owner_uid == os.getuid(), f"서비스가 다른 사용자로 실행 중입니다: {owner_uid}"

found = processes.agent_service_main_pids()
print("agent MainPIDs:", found, "target:", main_pid)
assert main_pid in found

adapter = LinuxAdapter()
success, message = protection.execute_kill(adapter, main_pid)
print("kill agent service ->", success, message)
assert success is False
time.sleep(0.5)
assert alive(main_pid), "보호 대상인 에이전트 서비스 프로세스가 종료됐습니다."

# 에이전트 서비스가 아닌 같은 사용자의 프로세스는 그대로 종료돼야 합니다.
other = subprocess.Popen(["sleep", "600"])
success, message = protection.execute_kill(adapter, other.pid)
print("kill ordinary process ->", success, message)
assert success is True
other.wait(timeout=5)

print("OK")
sys.exit(0)

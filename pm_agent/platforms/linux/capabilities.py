"""Linux 에이전트가 제공하는 기능 목록입니다."""


LINUX_CAPABILITIES = {
    "metrics": True,
    "process": True,
    "processKill": True,
    "serviceList": True,
    "serviceControl": True,
    "terminal": True,
    "fileList": False,  # 파일 목록 명령은 보안 검토로 제거했습니다.
    "hardwareDetail": True,
    "deviceManager": True,
    "selfUpdate": True,
    "selfUninstall": True,
}

"""Linux 에이전트가 제공하는 기능 목록입니다."""


LINUX_CAPABILITIES = {
    "metrics": True,
    "process": True,
    "processKill": True,
    "serviceList": True,
    "serviceControl": True,
    "terminal": True,
    "fileList": True,
    "hardwareDetail": True,
    "selfUpdate": True,
    "selfUninstall": True,
}

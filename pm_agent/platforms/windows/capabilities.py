"""Capability flags for the Windows agent adapter."""


WINDOWS_CAPABILITIES = {
    "metrics": True,
    "process": True,
    "processKill": True,
    "serviceList": False,
    "serviceControl": False,
    "terminal": False,
    "fileList": False,
    "hardwareDetail": True,
    "selfUpdate": True,
    "selfUninstall": True,
}

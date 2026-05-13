"""Capability flags for the Windows agent adapter."""


WINDOWS_CAPABILITIES = {
    "metrics": True,
    "process": False,
    "processKill": False,
    "serviceList": False,
    "serviceControl": False,
    "terminal": False,
    "fileList": False,
    "hardwareDetail": False,
    "selfUpdate": False,
    "selfUninstall": True,
}

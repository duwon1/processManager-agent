"""Capability flags for the Windows agent adapter."""


WINDOWS_CAPABILITIES = {
    "metrics": True,
    "process": True,
    "processKill": True,
    "serviceList": True,
    "serviceControl": True,
    "terminal": True,
    "fileList": False,
    "hardwareDetail": True,
    "deviceManager": True,
    "selfUpdate": True,
    "selfUninstall": True,
}

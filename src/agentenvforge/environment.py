"""Capturing the host environment of an experiment (hardware and software facts only).

Recorded so that timings and results can be interpreted later ("this ran on a 12-thread
laptop CPU without a GPU"). Nothing identifying is collected: no host name, user name, home
directory, MAC address or serial number. Every probe is best-effort and returns ``None``
rather than failing an experiment.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from agentenvforge.experiments.manifest import HostEnvironment
from agentenvforge.sandbox.base import BackendInfo


def capture_host_environment(backend: BackendInfo | None) -> HostEnvironment:
    """Describe this machine and the sandbox backend it uses."""
    return HostEnvironment(
        os=platform.system(),
        os_release=platform.release(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
        cpu_model=_cpu_model(),
        logical_cpus=os.cpu_count(),
        memory_bytes=_total_memory_bytes(),
        sandbox_backend=backend.name if backend else None,
        sandbox_backend_version=backend.version if backend else None,
        sandbox_details=dict(backend.details) if backend else {},
    )


def _run(argv: list[str]) -> str | None:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def _cpu_model() -> str | None:
    if sys.platform == "win32":
        return _windows_cpu_model() or platform.processor() or None
    elif sys.platform == "darwin":  # separate branches keep mypy's platform analysis exact
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor() or None
    else:
        return _linux_field("/proc/cpuinfo", "model name") or platform.processor() or None


def _total_memory_bytes() -> int | None:
    if sys.platform == "win32":
        return _windows_total_memory()
    elif sys.platform == "darwin":
        value = _run(["sysctl", "-n", "hw.memsize"])
        return int(value) if value and value.isdigit() else None
    else:
        value = _linux_field("/proc/meminfo", "MemTotal")
        kilobytes = value.split()[0] if value else ""
        return int(kilobytes) * 1024 if kilobytes.isdigit() else None


def _linux_field(path: str, name: str) -> str | None:
    """Value of the first ``name: value`` line of a /proc file."""
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith(name):
                return line.split(":", 1)[1].strip()
    except (OSError, IndexError):
        pass
    return None


if sys.platform == "win32":
    import ctypes
    import winreg

    def _windows_cpu_model() -> str | None:
        key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        except OSError:
            return None
        return str(value).strip() or None

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    def _windows_total_memory() -> int | None:
        status = _MemoryStatus()
        status.length = ctypes.sizeof(_MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.total_physical)

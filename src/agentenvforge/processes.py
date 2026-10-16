"""Identifying the process that owns a sandbox, so orphan cleanup never kills live work.

Each sandbox is labelled with an owner token ``<host>:<pid>``, where ``<host>`` is a short
hash of the host name (the name itself is never stored). A sandbox is an orphan only if its
owner is a process *on this host* that no longer exists. PID reuse can make a dead owner look
alive, which merely keeps a container that could have been removed; it can never cause a
live sandbox to be removed, which is the direction that matters.
"""

from __future__ import annotations

import hashlib
import os
import socket
import sys
from dataclasses import dataclass


def host_token() -> str:
    """A stable, non-identifying token for this host."""
    return hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class Owner:
    """The process that created a sandbox."""

    host: str
    pid: int

    @classmethod
    def current(cls) -> Owner:
        """The calling process."""
        return cls(host_token(), os.getpid())

    @classmethod
    def parse(cls, value: str | None) -> Owner | None:
        """Parse a ``<host>:<pid>`` token; ``None`` if absent or malformed."""
        if not value or ":" not in value:
            return None
        host, _, pid = value.rpartition(":")
        return cls(host, int(pid)) if pid.isdigit() and host else None

    def token(self) -> str:
        """Serialised form used as a container label value."""
        return f"{self.host}:{self.pid}"

    def is_local(self) -> bool:
        """True when the owner ran on this host."""
        return self.host == host_token()


def process_exists(pid: int) -> bool:
    """Return whether a process with ``pid`` currently exists on this host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_exists(pid)
    else:  # separate platform branches keep mypy's reachability analysis exact
        return _posix_process_exists(pid)


def _posix_process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0: existence check only, nothing is delivered
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


if sys.platform == "win32":

    def _windows_process_exists(pid: int) -> bool:
        # os.kill(pid, 0) must not be used on Windows: signal 0 is CTRL_C_EVENT there.
        import ctypes  # noqa: PLC0415 - Windows only
        from ctypes import wintypes  # noqa: PLC0415

        process_query_limited_information = 0x1000
        error_access_denied = 5
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return bool(exit_code.value == still_active)
        finally:
            kernel32.CloseHandle(handle)

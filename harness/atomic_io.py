"""One atomic small-file write: a new temp file beside the target, then a replace."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def write_atomic(path: Path, text: str, *, private: bool = False, prepare=None) -> None:
    """Replace `path` with `text` (UTF-8) via a new, uniquely named temp file in the same directory.

    The temp file is created with O_EXCL, so nothing already at its name is written through; a link at the target
    is replaced, not followed. `private` creates it owner-only (0o600) before any text goes in. A failed write
    removes the temp file and leaves the old file whole. The parent directory must exist. `prepare(temp_path)`, when
    given, runs on the new, still empty temp file before any text goes in (e.g. `owner_only_acl`).
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}-{os.urandom(4).hex()}.tmp")
    if private:
        tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666)
    try:
        if prepare is not None:
            prepare(tmp)
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def owner_only_acl(path: Path, *, inherit: bool = False) -> None:
    """On Windows, replace `path`'s DACL with one protected entry: full control for this process's user. Mode bits
    (0o600) mean nothing there, and a file otherwise inherits its folder's ACL, which on a non-system drive often lets
    every signed-in user read it. `inherit` also protects a directory's future children. Elsewhere it does nothing:
    `private` already made the file owner-only."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]

    def check(ok) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    token = wintypes.HANDLE()
    check(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)))  # TOKEN_QUERY
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser: ask for its size first
        buf = ctypes.create_string_buffer(size.value)
        check(advapi32.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)))
        sid_text = wintypes.LPWSTR()
        check(advapi32.ConvertSidToStringSidW(ctypes.c_void_p.from_buffer(buf).value, ctypes.byref(sid_text)))
        try:
            sid = sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)
    descriptor = ctypes.c_void_p()
    ace_flags = "OICI" if inherit else ""
    check(advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(f"D:P(A;{ace_flags};FA;;;{sid})", 1,
                                                                       ctypes.byref(descriptor), None))
    try:
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION: nothing inherited from the folder
        check(advapi32.SetFileSecurityW(str(path), 0x4 | 0x80000000, descriptor))
    finally:
        kernel32.LocalFree(descriptor)

"""Windows metadata-only directory access for owner Remote Control discovery.

All ancestor handles deny delete sharing while metadata is inspected, preventing
directory substitution during enumeration. Every reparse tag is rejected.
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path


class DiscoveryError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


EXCLUDED = frozenset({
    '.ssh', '.gnupg', '.aws', '.azure', '.config', '.codex', '.claude',
    '.git', 'node_modules', '.npm', '.yarn', '.pnpm-store', '.cache',
    '.venv', 'venv', 'env', '__pycache__', 'site-packages', '.tox',
    'build', 'dist', 'out', 'output', 'target', 'bin', 'obj', 'coverage',
    '.gradle', '.m2', '.nuget', 'packages', 'appdata', 'application data',
    'secrets', 'logs', 'backups',
    'windows', 'program files', 'program files (x86)', 'programdata',
    '$recycle.bin', 'system volume information', 'docker', 'credentials',
    'user data', 'profiles', 'browser', 'cache', 'caches',
})


def key(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def beneath(path: str, root: str) -> bool:
    try:
        return ntpath.commonpath([key(path), key(root)]) == key(root)
    except ValueError:
        return False


def lexical(value: str) -> str:
    value = os.path.expandvars(os.path.expanduser(value)).replace('/', '\\')
    drive, tail = ntpath.splitdrive(value)
    if (not drive or len(drive) != 2 or drive[1] != ':' or not drive[0].isalpha()
            or not tail.startswith('\\') or ':' in tail or '\x00' in value):
        raise DiscoveryError('absolute_local_path_required')
    parts = tail.split('\\')[1:]
    if not any(parts):
        raise DiscoveryError('drive_root')
    if any(p in ('.', '..') or p.endswith(('.', ' ')) for p in parts if p):
        raise DiscoveryError('path_alias')
    return ntpath.normpath(value)


def sensitive_paths(cfg) -> list[str]:
    paths = [str(cfg.data_dir), str(cfg.config_dir)]
    if cfg.backup.dir:
        paths.append(cfg.backup.dir)
    paths += [str(Path(p).parent) for p in cfg.provider_secret_files.values() if p]
    # Runner and provider credential files live under daemon data/config. Also
    # cover owner-registered credential paths outside those directories.
    for backend in cfg.backends.values():
        for field in ('api_key_file', 'token_file'):
            value = getattr(backend, field, '')
            if value:
                paths.append(str(Path(value).parent))
    if cfg.notify.token_file:
        paths.append(str(Path(cfg.notify.token_file).parent))
    if cfg.skills.reviewer_api_key_file:
        paths.append(str(Path(cfg.skills.reviewer_api_key_file).parent))
    return [os.path.expandvars(os.path.expanduser(p)) for p in paths]


@dataclass(frozen=True)
class Identity:
    path: str
    volume_serial: int
    file_id: str

    def record(self):
        return asdict(self)


class WindowsDirectories:
    def __init__(self, cfg):
        self.cfg = cfg

    def supported(self):
        if sys.platform != 'win32':
            raise DiscoveryError('unsupported_platform')

    def safe(self, path):
        if any(p.lower() in EXCLUDED for p in ntpath.splitdrive(path)[1].split('\\') if p):
            raise DiscoveryError('excluded_location')
        if any(beneath(path, p) for p in sensitive_paths(self.cfg)):
            raise DiscoveryError('sensitive_location')

    @contextmanager
    def opened(self, value):
        self.supported()
        from ctypes import wintypes as w
        path = lexical(value)
        self.safe(path)
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        api.CreateFileW.restype = w.HANDLE
        api.CloseHandle.argtypes = [w.HANDLE]
        api.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        api.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
        api.GetVolumeInformationByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.LPDWORD,
                                                    w.LPDWORD, w.LPDWORD, w.LPWSTR, w.DWORD]
        api.GetDriveTypeW.argtypes = [w.LPCWSTR]
        api.QueryDosDeviceW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
        drive = ntpath.splitdrive(path)[0]
        device = ctypes.create_unicode_buffer(32768)
        if (api.GetDriveTypeW(drive + '\\') != 3 or not api.QueryDosDeviceW(drive, device, len(device))
                or not device.value.lower().startswith('\\device\\harddiskvolume')):
            raise DiscoveryError('fixed_local_volume_required')

        class Attributes(ctypes.Structure):
            _fields_ = [('attributes', w.DWORD), ('tag', w.DWORD)]

        class FileIdentity(ctypes.Structure):
            _fields_ = [('volume', ctypes.c_ulonglong), ('id', ctypes.c_ubyte * 16)]

        handles = []
        current = drive + '\\'
        try:
            for part in ['', *ntpath.splitdrive(path)[1].strip('\\').split('\\')]:
                if part:
                    current = ntpath.join(current, part)
                # OPEN_REPARSE_POINT | BACKUP_SEMANTICS, READ_ATTRIBUTES,
                # share read/write, deliberately no FILE_SHARE_DELETE.
                handle = api.CreateFileW('\\\\?\\' + current, 0x80, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise DiscoveryError('directory_unavailable')
                handles.append(handle)
                attrs = Attributes()
                if not api.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)):
                    raise DiscoveryError('metadata_unavailable')
                if attrs.attributes & 0x400:
                    raise DiscoveryError('reparse_point')
                if not attrs.attributes & 0x10:
                    raise DiscoveryError('not_directory')
            fs = ctypes.create_unicode_buffer(64)
            if not api.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, fs, len(fs)):
                raise DiscoveryError('volume_unavailable')
            if fs.value.upper() not in ('NTFS', 'REFS'):
                raise DiscoveryError('ntfs_refs_required')
            final = ctypes.create_unicode_buffer(32768)
            size = api.GetFinalPathNameByHandleW(handle, final, len(final), 0)
            if not size or size >= len(final) or not final.value.startswith('\\\\?\\'):
                raise DiscoveryError('canonical_path_unavailable')
            canonical = lexical(final.value[4:])
            self.safe(canonical)
            ident = FileIdentity()
            if not api.GetFileInformationByHandleEx(handle, 18, ctypes.byref(ident), ctypes.sizeof(ident)):
                raise DiscoveryError('identity_unavailable')
            yield Identity(canonical, ident.volume, bytes(ident.id).hex())
        finally:
            for handle in reversed(handles):
                api.CloseHandle(handle)

    def roots(self, values):
        self.supported()
        if not isinstance(values, list) or len(values) > 8 or not all(isinstance(v, str) for v in values):
            raise DiscoveryError('up_to_eight_roots')
        identities = []
        for value in values:
            with self.opened(value) as ident:
                identities.append(ident)
        identities.sort(key=lambda i: (len(i.path), key(i.path)))
        result = []
        for ident in identities:
            if not any(beneath(ident.path, r.path) for r in result):
                result.append(ident)
        return result

    @contextmanager
    def entries(self, path):
        with os.scandir('\\\\?\\' + path) as entries:
            yield entries

    def entry(self, entry):
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
        return entry.name, bool(attrs & 0x10), bool(attrs & (0x2 | 0x4)), bool(attrs & 0x400)

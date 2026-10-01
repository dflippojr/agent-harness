"""Bounded, single-flight, owner-only Remote Control folder discovery.

Scanner state is ephemeral. Only explicit promotions enter the revisioned overlay.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import ntpath
import re
import secrets
import threading
import time
import sys
import os
from contextlib import contextmanager
from dataclasses import dataclass, field

from .config import PROJECT_NAME
from .discovery_paths import DiscoveryError, EXCLUDED, Identity, WindowsDirectories, beneath, key
from .managed_config import Envelope, ManagedConfigError, ManagedStore, parse_envelope

LIMITS = dict(visited_directories=20_000, candidates=500, seconds=30, errors=50,
              active_scans=1, expiry_seconds=900)
MARKERS = frozenset({'.git', 'pyproject.toml', 'package.json', 'cargo.toml', 'go.mod', 'pom.xml',
                     'settings.gradle', 'settings.gradle.kts', 'build.gradle', 'build.gradle.kts',
                     'cmakelists.txt', 'meson.build'})
SUFFIXES = ('.sln', '.slnx', '.csproj', '.vcxproj', '.code-workspace')


def marker(name):
    lower = name.lower()
    return lower in MARKERS or lower.endswith(SUFFIXES)


class FolderStore(ManagedStore):
    """Reuse #66 envelopes, locking, atomic commit and LKG for a separate document."""
    def __init__(self, directory):
        super().__init__(directory)
        self.active_path = self.data_dir / 'folders.json'
        self.lkg_path = self.data_dir / 'folders.lkg.json'
        self.lock_path = self.data_dir / 'folders.lock'
        self._protected = False

    @contextmanager
    def lock(self):
        if not self._protected:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if sys.platform == 'win32':
                import ctypes
                from ctypes import wintypes as w
                security = ctypes.WinDLL('advapi32', use_last_error=True)
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD,
                                                                                          ctypes.POINTER(w.LPVOID), w.LPDWORD]
                security.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, w.LPVOID]
                kernel.LocalFree.argtypes = [w.LPVOID]
                descriptor = w.LPVOID()
                # Protected DACL: directory owner and SYSTEM only; children
                # inherit this policy, including folder paths and audit files.
                if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                        'D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)', 1, ctypes.byref(descriptor), None):
                    raise DiscoveryError('owner_protection_unavailable', 409)
                try:
                    if not security.SetFileSecurityW(str(self.data_dir), 4 | 0x80000000, descriptor):
                        raise DiscoveryError('owner_protection_unavailable', 409)
                finally:
                    kernel.LocalFree(descriptor)
            else:
                os.chmod(self.data_dir, 0o700)
            self._protected = True
        with super().lock():
            yield

    def load(self):
        with self.lock():
            try:
                envelope = self.read_active()
                if envelope is None and self.lkg_path.exists():
                    envelope = self.read_lkg()
                    self.validate(envelope)
                    if envelope is not None:
                        self.commit(active=envelope)
                self.validate(envelope)
                return envelope or Envelope()
            except (ManagedConfigError, DiscoveryError):
                try:
                    envelope = self.read_lkg()
                    self.validate(envelope)
                    if envelope is None:
                        raise DiscoveryError('managed_folders_corrupt', 409)
                    self.commit(active=envelope)
                    return envelope
                except (ManagedConfigError, DiscoveryError):
                    raise DiscoveryError('managed_folders_corrupt', 409) from None

    def _read(self, path):
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
            values = document['values']
            envelope = parse_envelope({**document, 'values': {}})
            if not isinstance(values, dict):
                raise ValueError()
            envelope.values = values
            self.validate(envelope)
            return envelope
        except (OSError, ValueError, KeyError, TypeError):
            raise ManagedConfigError('managed folder document invalid') from None

    @staticmethod
    def validate(envelope):
        if envelope is None:
            return
        if not envelope.confirmed or envelope.unconfirmed:
            raise DiscoveryError('managed_folders_corrupt', 409)
        for slug, entry in envelope.values.items():
            if (not PROJECT_NAME.fullmatch(slug) or not isinstance(entry, dict)
                    or entry.get('owner_only') is not True):
                raise DiscoveryError('managed_folders_corrupt', 409)
            for field_name in ('identity', 'root'):
                obj = entry.get(field_name, {})
                if (set(obj) != {'path', 'volume_serial', 'file_id'} or not isinstance(obj['path'], str)
                        or not isinstance(obj['volume_serial'], int) or not isinstance(obj['file_id'], str)):
                    raise DiscoveryError('managed_folders_corrupt', 409)

    def save(self, old, values):
        new = Envelope(revision=old.revision + 1, values=values, confirmed=True, updated_at=time.time())
        self.validate(new)
        self.commit(active=new, lkg=old)
        return new


@dataclass
class Scan:
    id: str
    revision: int
    roots: list[Identity]
    depth: int
    actor: str
    created: float
    status: str = 'running'
    visited: int = 0
    candidates: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    truncated: bool = False
    reason: str = ''
    cancel: threading.Event = field(default_factory=threading.Event)


class FolderDiscovery:
    def __init__(self, rc, settings=None, directories=None, clock=time.monotonic):
        self.rc, self.cfg, self.settings = rc, rc.cfg, settings
        self.fs = directories or WindowsDirectories(self.cfg)
        self.clock = clock
        self.store = FolderStore(rc.dir)
        self.scans = {}
        self._guard = threading.RLock()
        self._active = None
        self._key = secrets.token_bytes(32)
        self._task = None

    def revision(self):
        if self.settings is None:
            return 0
        active = self.settings._active()
        return active.revision if active else 0

    def audit(self, action, actor='', scan=None, reason='', identity=None):
        event = dict(ts=time.time(), action=action, actor_id=actor, reason=reason,
                     scan_id=scan.id if scan else '', revision=scan.revision if scan else self.revision())
        if scan:
            event.update(visited=scan.visited, candidates=len(scan.candidates), errors=len(scan.errors))
        if identity:
            event['fingerprint'] = hmac.new(self._key, json.dumps(identity.record(), sort_keys=True).encode(),
                                            hashlib.sha256).hexdigest()
        self.rc.dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.rc.dir / 'discovery-audit.jsonl', 'a', encoding='utf-8') as output:
                output.write(json.dumps(event, sort_keys=True) + '\n')
        except OSError:
            # Fail closed on owner mutations/start when audit is unavailable.
            raise DiscoveryError('audit_unavailable', 409) from None

    def _expire(self):
        with self._guard:
            now = self.clock()
            for scan_id, scan in list(self.scans.items()):
                if now - scan.created >= LIMITS['expiry_seconds']:
                    scan.cancel.set()
                    del self.scans[scan_id]

    async def start(self, actor):
        self.fs.supported()
        config_lock = self.settings._lock if self.settings else self._guard
        with config_lock, self._guard:
            self._expire()
            if self._active is not None:
                return self.view(self._active)
            config = self.cfg.remote_control.discovery
            if not config.enabled:
                raise DiscoveryError('discovery_disabled', 409)
            roots = self.fs.roots(config.roots)
            if not roots:
                raise DiscoveryError('valid_root_required')
            scan = Scan(secrets.token_urlsafe(24), self.revision(), roots, config.max_depth, actor, self.clock())
            self.audit('scan_start', actor, scan)
            self.scans[scan.id] = scan
            self._active = scan
            self._task = asyncio.create_task(asyncio.to_thread(self._run, scan))
            asyncio.get_running_loop().call_later(LIMITS['expiry_seconds'], self._expire)
            return self.view(scan)

    def get(self, scan_id):
        self.fs.supported()
        with self._guard:
            self._expire()
            scan = self.scans.get(scan_id)
            if scan is None:
                raise DiscoveryError('scan_expired_or_unknown', 404)
            return scan

    def cancel(self, scan_id, actor):
        scan = self.get(scan_id)
        with self._guard:
            if not scan.cancel.is_set():
                self.audit('scan_cancel', actor, scan)
                scan.cancel.set()
            return self.view(scan)

    def _bound(self, scan):
        if scan.cancel.is_set():
            return 'cancelled'
        if self.clock() - scan.created >= LIMITS['seconds']:
            return 'time_limit'
        if scan.visited >= LIMITS['visited_directories']:
            return 'directory_limit'
        if len(scan.candidates) >= LIMITS['candidates']:
            return 'candidate_limit'
        return ''

    def _run(self, scan):
        stack = [(root.path, root, 0) for root in reversed(scan.roots)]
        try:
            while stack:
                reason = self._bound(scan)
                if reason:
                    scan.reason = reason
                    scan.truncated = reason != 'cancelled'
                    break
                path, root, depth = stack.pop()
                with self._guard:
                    scan.visited += 1
                try:
                    with self.fs.opened(path) as ident:
                        if not beneath(ident.path, root.path):
                            raise DiscoveryError('containment_changed')
                        markers, children, children_limited = [], [], False
                        with self.fs.entries(ident.path) as entries:
                            for entry in entries:
                                reason = self._bound(scan)
                                # visited == cap still permits metadata of the final
                                # visited directory, never traversal beyond the cap.
                                if reason and reason != 'directory_limit':
                                    scan.reason, scan.truncated = reason, reason != 'cancelled'
                                    break
                                name, directory, excluded, reparse = self.fs.entry(entry)
                                if reparse:
                                    continue
                                # .git may be hidden; only its presence is inspected.
                                if name.lower() == '.git':
                                    markers.append(name)
                                    continue
                                if excluded:
                                    continue
                                if marker(name):
                                    markers.append(name)
                                if directory and name.lower() not in EXCLUDED and not name.startswith('.'):
                                    child = ntpath.join(ident.path, name)
                                    try:
                                        self.fs.safe(child)
                                        if len(children) + len(stack) < LIMITS['visited_directories'] - scan.visited:
                                            children.append(child)
                                        else:
                                            children_limited = True
                                    except DiscoveryError:
                                        pass
                        with self._guard:
                            if markers and len(scan.candidates) < LIMITS['candidates']:
                                candidate_id = hmac.new(self._key, json.dumps([scan.id, scan.revision, root.record(),
                                                    ident.record()], sort_keys=True).encode(), hashlib.sha256).hexdigest()
                                scan.candidates[candidate_id] = dict(identity=ident, root=root,
                                                                      markers=sorted(set(markers)))
                        if not markers and depth < scan.depth and not scan.reason:
                            stack.extend((child, root, depth + 1) for child in reversed(children))
                            if children_limited:
                                scan.truncated = True
                except (DiscoveryError, OSError) as error:
                    with self._guard:
                        if len(scan.errors) < LIMITS['errors']:
                            # Directory names may contain usernames/secrets; never
                            # echo arbitrary names even in partial-error metadata.
                            relative = ntpath.relpath(path, root.path)
                            location = '.' if relative == '.' else '/'.join('*' for _ in relative.split('\\'))
                            scan.errors.append(dict(location=location, code=error.code if isinstance(error, DiscoveryError)
                                                    else 'directory_unavailable'))
                if scan.reason:
                    break
            with self._guard:
                scan.status = 'cancelled' if scan.cancel.is_set() else 'finished'
                if scan.truncated and not scan.reason:
                    scan.reason = 'directory_limit'
                if not scan.reason and scan.cancel.is_set():
                    scan.reason = 'cancelled'
                self.audit('scan_finish', scan.actor, scan, scan.reason)
        except Exception:
            with self._guard:
                scan.status, scan.reason = 'failed', 'scan_unavailable'
        finally:
            with self._guard:
                if self._active is scan:
                    self._active = None

    def entries(self):
        return self.store.load().values

    @contextmanager
    def checked(self, entry):
        self.fs.supported()
        roots = self.fs.roots(self.cfg.remote_control.discovery.roots)
        expected_root = Identity(**entry['root'])
        if expected_root not in roots:
            raise DiscoveryError('root_changed', 409)
        expected = Identity(**entry['identity'])
        with self.fs.opened(expected_root.path) as root:
            if root != expected_root:
                raise DiscoveryError('root_identity_changed', 409)
            with self.fs.opened(expected.path) as current:
                if current != expected or not beneath(current.path, root.path):
                    raise DiscoveryError('folder_identity_changed', 409)
                yield current

    def git_present(self, identity):
        with self.fs.entries(identity.path) as entries:
            return any((info := self.fs.entry(e))[0].lower() == '.git' and not info[3] for e in entries)

    def promote(self, scan_id, candidate_id, slug, confirmed_path, confirmed_markers, actor):
        try:
            scan = self.get(scan_id)
            with self._guard:
                candidate = scan.candidates.get(candidate_id)
            if candidate is None:
                raise DiscoveryError('unknown_candidate', 404)
            if not self.cfg.remote_control.discovery.enabled or self.revision() != scan.revision:
                raise DiscoveryError('config_revision_changed', 409)
            if (confirmed_path != candidate['identity'].path or confirmed_markers != candidate['markers']):
                raise DiscoveryError('confirmation_required')
            if not isinstance(slug, str) or not PROJECT_NAME.fullmatch(slug):
                raise DiscoveryError('invalid_slug')
            entry = dict(identity=candidate['identity'].record(), root=candidate['root'].record(), owner_only=True)
            # Keep config revision stable across validation and the commit.
            config_lock = self.settings._lock if self.settings else self._guard
            with config_lock, self.store.lock():
                if self.clock() - scan.created >= LIMITS['expiry_seconds']:
                    raise DiscoveryError('scan_expired_or_unknown', 404)
                if self.revision() != scan.revision:
                    raise DiscoveryError('config_revision_changed', 409)
                with self.checked(entry) as ident:
                    if self.rc.rc.spawn == 'worktree' and not self.git_present(ident):
                        raise DiscoveryError('git_required', 409)
                    old = self.store.load()
                    for existing_slug, value in old.values.items():
                        if value['identity'] == entry['identity']:
                            return dict(slug=existing_slug, already_added=True)
                        if key(value['identity']['path']) == key(ident.path):
                            raise DiscoveryError('managed_path_conflict', 409)
                    if slug in self.cfg.projects or slug in self.rc.rc.folders or slug in old.values:
                        raise DiscoveryError('slug_conflict', 409)
                    configured = list(self.rc.rc.folders.values()) + [p.repo for p in self.cfg.projects.values()]
                    if any(key(p) == key(ident.path) for p in configured if p):
                        raise DiscoveryError('configured_duplicate', 409)
                    self.audit('promotion', actor, scan, identity=ident)
                    self.store.save(old, {**old.values, slug: entry})
                    return dict(slug=slug, already_added=False)
        except (DiscoveryError, OSError) as error:
            code = error.code if isinstance(error, DiscoveryError) else 'directory_unavailable'
            self.audit('promotion_refusal', actor, reason=code)
            raise DiscoveryError(code, error.status if isinstance(error, DiscoveryError) else 409) from None

    async def remove(self, slug, actor):
        self.fs.supported()
        async with self.rc._lock:
            with self.store.lock():
                old = self.store.load()
                if slug not in old.values:
                    raise DiscoveryError('managed_folder_unknown', 404)
                if self.rc._trust_prompt_open(slug) or self.rc._alive(self.rc._load().get(slug, {})):
                    self.audit('removal_refusal', actor, reason='entry_active')
                    raise DiscoveryError('entry_active', 409)
                self.audit('removal', actor, identity=Identity(**old.values[slug]['identity']))
                self.store.save(old, {k: v for k, v in old.values.items() if k != slug})
                return dict(removed=True)

    def view(self, scan):
        from .remote_control import _norm_path, trusted_folders
        with self._guard:
            configured = {key(p) for p in self.rc.rc.folders.values()}
            configured.update(key(p.repo) for p in self.cfg.projects.values() if p.repo)
            managed = {key(e['identity']['path']) for e in self.entries().values()}
            trusted = trusted_folders(self.rc.claude_json)
            candidates = []
            for candidate_id, candidate in scan.candidates.items():
                path = candidate['identity'].path
                git = any(m.lower() == '.git' for m in candidate['markers'])
                needs_git = self.rc.rc.spawn == 'worktree'
                slug = re.sub('[^a-z0-9._-]', '-', ntpath.basename(path).lower()).strip('.-_')[:64] or 'folder'
                candidates.append(dict(id=candidate_id, path=path, suggested_slug=slug, markers=candidate['markers'],
                                       git=git, requires_git=needs_git, launchable=git or not needs_git,
                                       reason='git_required' if needs_git and not git else '',
                                       configured_duplicate=key(path) in configured, promoted=key(path) in managed,
                                       trusted=_norm_path(path) in trusted))
            return dict(id=scan.id, revision=scan.revision, status=scan.status, visited=scan.visited,
                        candidates=candidates, errors=list(scan.errors), truncated=scan.truncated, reason=scan.reason,
                        expires_in=max(0, int(LIMITS['expiry_seconds'] - (self.clock() - scan.created))), limits=dict(LIMITS))

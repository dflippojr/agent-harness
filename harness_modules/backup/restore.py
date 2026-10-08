"""Verify and restore a nightly backup (#374): python -m harness_modules.backup.restore verify|restore <folder>

A local tool like `harness.doctor`, run on the server machine against the configured `data_dir` with the daemon
stopped (a restore replaces the daemon's own files). What a backup folder holds is `BackupService.backup_sync`'s:

- `harness.sqlite3`: the main store;
- `apps/<app_id>.sqlite3`: one per App store, Web's `app-web` included;
- `transcripts.zip` (the owner's), `transcripts/users/<user_id>.zip` (members') and `transcripts/apps/<app_id>.zip`;
- `config/` (`harness.yaml`, `harness.local.yaml`, `projects.yaml`) and the managed-config overlay files.

`verify` checks every store opens read-only, passes `PRAGMA integrity_check` and isn't at a schema newer than this
code, every zip passes its CRC check and holds only relative paths, and every App and member file is named by a
valid id. `restore` runs the same checks, refuses while the daemon is running, and without `--apply` only prints
what it would replace. With `--apply` it first moves everything it replaces into
`<data_dir>/restore-<timestamp>-previous/` (nothing is deleted), then copies the backup in. Config files and the
overlay are restored only with `--include-config`, because they may hold another machine's paths. An App store or
App transcript archive whose App no longer exists in the restored main store is skipped with a warning, so a restore
can't bring back erased App data on its own. See docs/INSTALL.md, "Backups and restore".
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from harness.modules import (APP_STORE_FILE, OWNER_USER_ID, ROOT, WEB_APP_ID, app_dir,
                            migrations, remove_tree, storage)

from .member_key import KEY_FILE, expected_fingerprint, fingerprint, key_dir, write_private

CONFIG_FILES = ("harness.yaml", "harness.local.yaml", "projects.yaml")
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
PREVIOUS_PREFIX = "restore-"
PREVIOUS_SUFFIX = "-previous"
MANIFEST = "RESTORE.txt"


class _NoConfig:
    data_dir = "."


class RestoreRefused(RuntimeError):
    """The backup failed verification or the daemon is running: nothing was changed."""


@dataclass
class Item:
    """One thing a restore puts in place: a file copied to `dest`, or a zip extracted into the folder `dest`."""
    kind: str        # "store", "transcripts" or "config"
    source: Path
    dest: Path
    label: str


@dataclass
class Plan:
    folder: Path
    items: list[Item] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# verification
def _open_copy(path: Path) -> sqlite3.Connection:
    """Open a backup copy without writing anything next to it: `immutable` skips the -wal/-shm files a read-only
    open of a WAL database would otherwise create."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _sqlite_problem(path: Path, latest: int) -> str:
    try:
        conn = _open_copy(path)
    except sqlite3.Error as e:
        return f"does not open: {e}"
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            return f"failed its integrity check: {check}"
        current = migrations.user_version(conn)
    except sqlite3.Error as e:
        return f"is not a readable SQLite database: {e}"
    finally:
        conn.close()
    if current > latest:
        return migrations.too_new_message(current, latest)
    return ""


def _zip_problem(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                parts = PurePosixPath(name).parts
                if name.startswith(("/", "\\")) or ".." in parts or ":" in name or "\\" in name:
                    return f"holds an unsafe path: {name!r}"
            bad = archive.testzip()
    except (OSError, zipfile.BadZipFile, EOFError, zlib.error) as e:
        return f"is not a readable zip: {e}"
    return f"failed its CRC check at {bad!r}" if bad else ""


def _valid_app_id(name: str) -> bool:
    try:
        app_dir(Path("."), name)
    except ValueError:
        return False
    return True


def _valid_user_id(name: str) -> bool:
    if name == OWNER_USER_ID:
        return False
    try:
        storage.user_root(_NoConfig, name)
    except storage.ContainmentError:
        return False
    return True


def verify(folder: Path) -> list[str]:
    """Every problem that makes `folder` unsafe to restore; an empty list means it verified."""
    folder = Path(folder)
    if not folder.is_dir():
        return [f"{folder} is not a folder"]
    problems: list[str] = []
    try:
        latest = migrations.latest_version(migrations.discover())
    except migrations.MigrationError as e:
        return [f"could not load this code's migrations: {e}"]
    main = folder / "harness.sqlite3"
    stores = [main] if main.is_file() else []
    if not stores:
        problems.append("harness.sqlite3 (the main store) is missing")
    apps = folder / "apps"
    if apps.is_dir():
        for path in sorted(apps.iterdir()):
            if path.name.endswith(SQLITE_SIDECARS):
                continue  # left by something that opened a copy; `immutable` reads ignore them
            if path.suffix != ".sqlite3" or not path.is_file() or not _valid_app_id(path.stem):
                problems.append(f"apps/{path.name} is not named <App id>.sqlite3")
            else:
                stores.append(path)
    for path in stores:
        problem = _sqlite_problem(path, latest)
        if problem:
            problems.append(f"{path.relative_to(folder).as_posix()} {problem}")
    zips = [folder / "transcripts.zip"] if (folder / "transcripts.zip").is_file() else []
    for sub, valid in (("users", _valid_user_id), ("apps", _valid_app_id)):
        root = folder / "transcripts" / sub
        for path in sorted(root.iterdir()) if root.is_dir() else ():
            if path.suffix != ".zip" or not path.is_file() or not valid(path.stem):
                problems.append(f"transcripts/{sub}/{path.name} is not named <{sub[:-1]} id>.zip")
            else:
                zips.append(path)
    for path in zips:
        problem = _zip_problem(path)
        if problem:
            problems.append(f"{path.relative_to(folder).as_posix()} {problem}")
    return problems


# planning
def _live_apps(main_store: Path) -> set[str] | None:
    """Ids of the Apps the backup's main store still registers (not erased); None if it has no App registry."""
    conn = _open_copy(main_store)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
        if not columns:
            return None
        where = " WHERE erased_at IS NULL" if "erased_at" in columns else ""
        return {row[0] for row in conn.execute(f"SELECT id FROM api_keys{where}")}
    finally:
        conn.close()


def plan(cfg, folder: Path, *, include_config: bool = False, config_dir: Path | None = None) -> Plan:
    """What a restore of a verified `folder` puts where. Doesn't touch anything."""
    folder = Path(folder)
    data = Path(cfg.data_dir)
    p = Plan(folder)
    p.items.append(Item("store", folder / "harness.sqlite3", Path(cfg.db_path), "main store"))
    _plan_member_key(cfg, folder, p)
    live = _live_apps(folder / "harness.sqlite3")

    def app_exists(app_id: str) -> bool:
        return app_id == WEB_APP_ID or live is None or app_id in live

    for path in sorted((folder / "apps").glob("*.sqlite3")):
        if app_exists(path.stem):
            p.items.append(Item("store", path, app_dir(data, path.stem) / APP_STORE_FILE, f"App store {path.stem}"))
        else:
            p.warnings.append(f"skipped App store {path.stem}: the App no longer exists in the restored main store")
    if (folder / "transcripts.zip").is_file():
        p.items.append(Item("transcripts", folder / "transcripts.zip",
                            storage.transcripts_dir(cfg, OWNER_USER_ID), "owner's transcripts"))
    for path in sorted((folder / "transcripts" / "users").glob("*.zip")):
        p.items.append(Item("transcripts", path, storage.transcripts_dir(cfg, path.stem),
                            f"member {path.stem}'s transcripts"))
    for path in sorted((folder / "transcripts" / "apps").glob("*.zip")):
        if app_exists(path.stem):
            p.items.append(Item("transcripts", path, storage.transcripts_dir(cfg, OWNER_USER_ID, path.stem),
                                f"App {path.stem}'s transcripts"))
        else:
            p.warnings.append(f"skipped App {path.stem}'s transcripts: the App no longer exists in the restored "
                              "main store")
    if include_config:
        config_dir = Path(config_dir or os.environ.get("HARNESS_CONFIG_DIR") or ROOT / "config")
        for name in CONFIG_FILES:
            if (folder / "config" / name).is_file():
                p.items.append(Item("config", folder / "config" / name, config_dir / name, f"config {name}"))
        for path in sorted(folder.glob("managed-config*.json")):
            p.items.append(Item("config", path, data / path.name, f"managed config {path.name}"))
    return p


def _plan_member_key(cfg, folder: Path, p: Plan) -> None:
    digest = expected_fingerprint(folder / "harness.sqlite3")
    reason = "the database has no member-key fingerprint (older backup or no key copied)"
    if digest:
        # Only a SHA-256 digest may become a filename, even for an edited database.
        import re
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            reason = "the database's member-key fingerprint is invalid"
        else:
            source, reason = _matching_member_key(cfg, folder, digest)
            if source:
                p.items.append(Item("member_key", source, Path(cfg.data_dir) / KEY_FILE, "member encryption key"))
                return
    p.warnings.append(f"{reason}; members must re-add their API keys if their existing key cannot decrypt them")


def _matching_member_key(cfg, folder: Path, digest: str) -> tuple[Path | None, str]:
    try:
        source = key_dir(cfg, folder.parent) / f"{digest}.key"
    except ValueError as e:
        return None, f"the member-key directory is invalid: {e}"
    try:
        matches = fingerprint(source.read_bytes()) == digest
    except (OSError, ValueError):
        return None, "the matching member-key copy is missing, unreadable or invalid"
    if not matches:
        return None, "the member-key copy's fingerprint does not match the database"
    return source, ""


# the daemon
def daemon_running(cfg) -> str:
    """Why the daemon looks like it's running ('' if it doesn't): it answers health checks on the configured port,
    or something holds a store's write lock."""
    import httpx

    if cfg.port:
        try:
            httpx.get(f"http://127.0.0.1:{cfg.port}/health", timeout=2)
            return f"the daemon answers on port {cfg.port}"
        except httpx.HTTPError:
            pass
    stores = [Path(cfg.db_path)]
    apps = Path(cfg.data_dir) / "apps"
    stores += sorted(apps.glob(f"*/{APP_STORE_FILE}")) if apps.is_dir() else []
    for path in stores:
        if not path.is_file():
            continue
        try:
            conn = sqlite3.connect(str(path), timeout=0, isolation_level=None)
        except sqlite3.Error:
            continue  # damaged or unreadable: what a restore is for, and no daemon can be using it either
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as e:
            if "locked" in str(e) or "busy" in str(e):
                return f"{path} is locked by another process ({e})"
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return ""


# applying
def _occupied(item: Item) -> list[Path]:
    """The live paths `item` replaces: a store and its WAL files, a whole transcripts folder, or a config file."""
    if item.kind == "store":
        paths = [item.dest] + [item.dest.with_name(item.dest.name + s) for s in SQLITE_SIDECARS]
    else:
        paths = [item.dest]
    return [p for p in paths if p.exists() or p.is_symlink()]


def _previous_path(previous: Path, path: Path, data: Path) -> Path:
    try:
        return previous / path.relative_to(data)
    except ValueError:  # a config file outside data_dir
        return previous / "config" / path.name


def apply(cfg, p: Plan, now: float | None = None) -> Path:
    """Move what `p` replaces into restore-<timestamp>-previous/, then copy the backup in. Returns that folder. On a
    failure part way, puts back what was moved and re-raises."""
    data = Path(cfg.data_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    previous = data / f"{PREVIOUS_PREFIX}{stamp}{PREVIOUS_SUFFIX}"
    n = 1
    while previous.exists():
        n += 1
        previous = data / f"{PREVIOUS_PREFIX}{stamp}-{n}{PREVIOUS_SUFFIX}"
    previous.mkdir(parents=True)
    moved: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        for item in p.items:
            for path in _occupied(item):
                target = _previous_path(previous, path, data)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
                moved.append((path, target))
        for item in p.items:
            item.dest.parent.mkdir(parents=True, exist_ok=True)
            created.append(item.dest)
            if item.kind == "transcripts":
                item.dest.mkdir()
                with zipfile.ZipFile(item.source) as archive:
                    archive.extractall(item.dest)
            elif item.kind == "member_key":
                content = item.source.read_bytes()
                if fingerprint(content) != expected_fingerprint(p.folder / "harness.sqlite3"):
                    raise RestoreRefused("member-key fingerprint changed after planning")
                write_private(item.dest, content)
            else:
                shutil.copy2(item.source, item.dest)
    except BaseException:
        _roll_back(created, moved)
        if not any(previous.rglob("*")):
            remove_tree(previous)
        raise
    lines = [f"Restored from {p.folder} at {time.strftime('%Y-%m-%d %H:%M:%S')}.", "",
             "Moved here (original location):"]
    lines += [f"  {original}" for original, _ in moved] or ["  (nothing)"]
    lines += ["", "Put in place from the backup:"] + [f"  {path}" for path in created]
    (previous / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return previous


def _roll_back(created: list[Path], moved: list[tuple[Path, Path]]) -> None:
    for path in reversed(created):
        if path.is_dir() and not path.is_symlink():
            remove_tree(path)
        elif path.exists():
            path.unlink()
    for original, target in reversed(moved):
        if target.exists() or target.is_symlink():
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(original))


def restore(cfg, folder: Path, *, apply_changes: bool = False, include_config: bool = False,
            config_dir: Path | None = None, out=print) -> Path | None:
    """Verify `folder`, refuse while the daemon runs, then print the plan and, with `apply_changes`, carry it out.
    Raises RestoreRefused (nothing changed) on any problem. Returns the restore-...-previous folder when applied."""
    problems = verify(folder)
    if problems:
        raise RestoreRefused("the backup failed verification:\n" + "\n".join(f"  {x}" for x in problems))
    running = daemon_running(cfg)
    if running:
        raise RestoreRefused(f"stop the daemon first: {running}")
    p = plan(cfg, folder, include_config=include_config, config_dir=config_dir)
    for warning in p.warnings:
        out(f"WARN  {warning}")
    for item in p.items:
        verb = "replace" if _occupied(item) else "create "
        what = "folder" if item.kind == "transcripts" else "file"
        out(f"{verb} {what} {item.dest}  <- {item.source} ({item.label})")
    if not include_config and ((Path(folder) / "config").is_dir() or any(Path(folder).glob("managed-config*.json"))):
        out("Config files and the managed overlay are not restored; add --include-config to restore them.")
    if not apply_changes:
        out("Dry run: nothing was changed. Add --apply to restore.")
        return None
    previous = apply(cfg, p)
    out(f"Restored. Everything replaced was moved to {previous} (listed in its {MANIFEST}).")
    out(f"To undo: with the daemon stopped, move the restored files out of the way and move the contents of "
        f"{previous} back into {cfg.data_dir}" + (" (its config/ folder back into the config folder)"
                                                   if include_config else "") + ".")
    return previous


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness_modules.backup.restore",
                                 description="Verify or restore a nightly backup folder (backup.dir/<date>)")
    ap.add_argument("--config-dir", help="the harness config folder (default: HARNESS_CONFIG_DIR or config/)")
    sub = ap.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", help="check a backup folder; changes nothing")
    v.add_argument("folder", type=Path)
    r = sub.add_parser("restore", help="restore a backup folder into the configured data_dir (daemon stopped)")
    r.add_argument("folder", type=Path)
    r.add_argument("--apply", action="store_true", help="make the changes (default: print them only)")
    r.add_argument("--include-config", action="store_true",
                   help="also restore config/*.yaml and the managed-config overlay")
    args = ap.parse_args(argv)

    if args.command == "verify":
        problems = verify(args.folder)
        for problem in problems:
            print(f"FAIL  {problem}")
        print(f"{args.folder}: " + ("failed verification" if problems else "OK"))
        return 1 if problems else 0

    import harness.modules as core
    cfg = core.load_config(args.config_dir)
    try:
        restore(cfg, args.folder, apply_changes=args.apply, include_config=args.include_config,
                config_dir=args.config_dir)
    except RestoreRefused as e:
        print(f"Refused, nothing changed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Local, unsigned full manifests. Validation never fetches or verifies artifacts."""
from __future__ import annotations

import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.exceptions import NoSuchResource

from harness.modules import ROOT

SCHEMA = ROOT / "docs" / "marketplace-manifest.schema.json"
EXAMPLE = ROOT / "docs" / "hub.entries.example.json"
FILENAME = "hub.entries.json"


class EntriesError(ValueError):
    """A named local file or manifest error, without echoing its contents."""


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


def load(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError):
        raise EntriesError("hub.entries: unreadable file") from None
    try:
        document = json.loads(text)
    except (ValueError, UnicodeError):
        raise EntriesError("hub.entries: invalid JSON") from None
    if not isinstance(document, dict) or set(document) != {"entries"} or not isinstance(document["entries"], list):
        raise EntriesError("hub.entries: expected only an entries array")
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")),
                                      format_checker=FormatChecker(), registry=Registry(retrieve=_no_remote))
    seen = set()
    for index, entry in enumerate(document["entries"]):
        error = next(validator.iter_errors(entry), None)
        if error is not None:
            field = ".".join(str(part) for part in error.path) or "manifest"
            raise EntriesError(f"entries[{index}].{field}: {error.validator} validation failed")
        app_id = entry["app"]["app_id"]
        if app_id in seen:
            raise EntriesError(f"entries[{index}].app.app_id: duplicate id")
        seen.add(app_id)
    return document["entries"]


def initialize(config_dir: Path) -> Path:
    load(EXAMPLE)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / FILENAME
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(EXAMPLE.read_text(encoding="utf-8"))
    except FileExistsError:
        raise EntriesError("hub.entries: file already exists; refusing to overwrite") from None
    return path


def _command(args):
    config_dir = Path(args.config_dir or os.environ.get("HARNESS_CONFIG_DIR") or ROOT / "config").expanduser()
    try:
        if args.entries_action == "init":
            print(initialize(config_dir))
        else:
            path = Path(args.file).expanduser() if args.file else config_dir / FILENAME
            print(json.dumps({"valid": True, "entries": len(load(path))}))
    except (EntriesError, OSError) as exc:
        print(str(exc))
        return 1
    return 0


def add_cli(groups):
    entries = groups["hub"].add_parser("entries", help="manage local unsigned manifests").add_subparsers(
        dest="entries_action", required=True)
    for action in ("init", "validate"):
        parser = entries.add_parser(action)
        parser.add_argument("--config-dir", help="daemon config directory (or HARNESS_CONFIG_DIR)")
        if action == "validate":
            parser.add_argument("file", nargs="?")
        parser.set_defaults(local_handler=_command)

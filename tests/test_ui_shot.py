"""scripts/ui-shot.mjs starts Edge from its fixed install path, never through a PATH lookup."""

from __future__ import annotations

import ntpath
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ui-shot.mjs"


def test_ui_shot_spawns_edge_by_absolute_native_path():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.findall(r"\b(?:spawn|spawnSync|exec|execSync|execFile|execFileSync)\(\s*([^,)]+)", text) == ["EDGE"]
    raw = re.search(r"^const EDGE = String\.raw`([^`]*)`;", text, re.M)
    if raw:
        path = raw.group(1)  # String.raw keeps the backslashes as written
    else:
        literal = re.search(r'^const EDGE = "((?:[^"\\]|\\.)*)";', text, re.M).group(1)
        path = re.sub(r"\\(.)", r"\1", literal)  # undo the JS string escapes
    assert ntpath.splitdrive(path)[0] and ntpath.isabs(path)
    assert path == ntpath.normpath(path)  # native separators, no relative parts
    assert ntpath.basename(path).lower() == "msedge.exe"

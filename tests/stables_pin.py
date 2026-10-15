"""The pinned Stables release, for tests that check the Server against what the client calls (#334).

Stables lives in dflippojr/stables. `tools/stables/pin.json` names the release the Server deploys and tests, and
`tools/stables/api-calls.json` records what the Server's tests need from it: its api() calls and path literals, its
browser storage keys, its build id and its snippet languages. `tools/stables/pin.py` writes both from a checkout of
the tag, and CI's UI-test job checks them against that checkout.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parents[1] / "tools" / "stables"
PIN = json.loads((DIRECTORY / "pin.json").read_text(encoding="utf-8"))
FACTS = json.loads((DIRECTORY / "api-calls.json").read_text(encoding="utf-8"))

_spec = importlib.util.spec_from_file_location("stables_pin_tool", DIRECTORY / "pin.py")
assert _spec and _spec.loader
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

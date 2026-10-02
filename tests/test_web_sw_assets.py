"""The service worker's ASSETS list covers every web module, so none is missed by the network-first/offline shell (#258)."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "harness" / "web"


def _assets() -> list[str]:
    worker = (WEB / "sw.js").read_text(encoding="utf-8")
    block = re.search(r"const ASSETS = \[(.*?)\];", worker, re.S)
    assert block, "ASSETS array missing from sw.js"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_sw_assets_list_every_module():
    assets = set(_assets())
    modules = [WEB / "app.js", *WEB.rglob("*.mjs")]
    missing = sorted("/" + p.relative_to(WEB).as_posix() for p in modules if "/" + p.relative_to(WEB).as_posix() not in assets)
    assert not missing, f"add to ASSETS in harness/web/sw.js: {missing}"


def test_sw_assets_exist():
    absent = [a for a in _assets() if a != "/" and not (WEB / a.lstrip("/")).is_file()]
    assert not absent, f"ASSETS lists files that don't exist: {absent}"

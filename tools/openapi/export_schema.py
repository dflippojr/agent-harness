"""Export the daemon's full-profile OpenAPI contract without reading host configuration or starting services.

    python tools/openapi/export_schema.py            print the document
    python tools/openapi/export_schema.py --write    rewrite the committed tools/openapi/openapi.json
    python tools/openapi/export_schema.py --check    fail when the committed document is stale
"""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harness.api import create_app  # noqa: E402
from harness.config import Config, ModelConfig, Project, SandboxConfig  # noqa: E402
from harness.manager import Manager  # noqa: E402

COMMITTED = Path(__file__).resolve().with_name("openapi.json")


def schema() -> dict:
    with TemporaryDirectory(prefix="harness-openapi-") as scratch:
        root = Path(scratch)
        cfg = Config(host="127.0.0.1", port=8100, data_dir=root / "data", repos_dir=root / "repos",
                     config_dir=root / "config", default_model="test",
                     models={"test": ModelConfig("test", "http://127.0.0.1:1")},
                     sandbox=SandboxConfig(), projects={"scratch": Project("scratch")})
        manager = Manager(cfg)
        try:
            return create_app(manager).openapi()
        finally:
            manager.db.close()


def _as_javascript(value):
    """Whole floats as integers, the way JavaScript's JSON.stringify writes them, so clients can re-serialise the
    document byte for byte (Stables pins it and regenerates its types from it)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _as_javascript(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_javascript(item) for item in value]
    return value


def document() -> str:
    return json.dumps(_as_javascript(schema()), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = document()
    if "--write" in argv:
        COMMITTED.write_text(text, encoding="utf-8", newline="\n")
    elif "--check" in argv:
        if COMMITTED.read_text(encoding="utf-8") != text:
            print(f"{COMMITTED.relative_to(ROOT).as_posix()} is stale: run python tools/openapi/export_schema.py --write",
                  file=sys.stderr)
            return 1
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

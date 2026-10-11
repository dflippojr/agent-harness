"""Export the daemon's full-profile OpenAPI contract without reading host configuration or starting services."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harness.api import create_app  # noqa: E402
from harness.config import Config, ModelConfig, Project, SandboxConfig  # noqa: E402
from harness.manager import Manager  # noqa: E402


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


if __name__ == "__main__":
    print(json.dumps(schema(), indent=2, sort_keys=True, ensure_ascii=False))

"""Resolve benchmark-owned env references, then exec the native headless CLI."""
import json
import os
from pathlib import Path
import sys

config = Path("/opt/bakeoff/config.json").read_text()
for key in ("LLM_BASE_URL", "LLM_MODEL"):
    config = config.replace("{env:" + key + "}", os.environ[key])
home = Path(os.environ["HERMES_HOME"])
home.mkdir(parents=True, exist_ok=True)
# JSON is also YAML, understood by Hermes' config reader.
(home / "config.yaml").write_text(json.dumps(json.loads(config)))
os.environ["OPENAI_BASE_URL"] = os.environ["LLM_BASE_URL"]
os.environ["OPENAI_API_KEY"] = "local"
os.environ["TERMINAL_CWD"] = "/workspace"
os.execvp("hermes", ["hermes", "chat", "--provider", "custom", "--model", os.environ["LLM_MODEL"],
                       "--format", "stream-json", *sys.argv[1:]])

"""Run the native local agent and export its session events for the adapter."""
import json
import os
from pathlib import Path
import subprocess
import sys

config = Path("/opt/bakeoff/config.json").read_text()
for key in ("LLM_BASE_URL", "LLM_MODEL"):
    config = config.replace("{env:" + key + "}", os.environ[key])
path = Path(os.environ["OPENCLAW_CONFIG_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(json.loads(config)))
proc = subprocess.run(["openclaw", "agent", "--local", "--agent", "main", "--session-id", "bakeoff",
                       "--message", sys.argv[1], "--json"], capture_output=True, text=True)
sys.stderr.write(proc.stderr)
try:
    print(json.dumps(json.loads(proc.stdout)))
except ValueError:
    sys.stderr.write(proc.stdout)
for log in Path(os.environ["OPENCLAW_STATE_DIR"]).glob("agents/*/sessions/*.jsonl"):
    for line in log.read_text().splitlines():
        try:
            print(json.dumps(json.loads(line)))
        except ValueError:
            pass
sys.exit(proc.returncode)

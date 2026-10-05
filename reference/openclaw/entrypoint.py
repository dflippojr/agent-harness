"""Run the native local agent and export its session events for the adapter."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys

config = Path("/opt/bakeoff/config.json").read_text()
for key in ("LLM_BASE_URL", "LLM_MODEL"):
    config = config.replace("{env:" + key + "}", os.environ[key])
path = Path(os.environ["OPENCLAW_CONFIG_PATH"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(json.loads(config)))
# OpenClaw's pinned safe-write helper needs Linux renameat2 semantics, which
# Docker Desktop's Windows bind mounts do not provide. Run on the container's
# filesystem, then return the resulting tree (including deletions) to the mount.
workspace, fixture = Path("/workspace"), Path("/fixture")
shutil.copytree(fixture, workspace, dirs_exist_ok=True)
proc = subprocess.run(["openclaw", "agent", "--local", "--agent", "main", "--session-id", "bakeoff",
                       "--message", sys.argv[1], "--json"], capture_output=True, text=True)
sys.stderr.write(proc.stderr)
for child in fixture.iterdir():
    if not (workspace / child.name).exists():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
shutil.copytree(workspace, fixture, dirs_exist_ok=True)
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

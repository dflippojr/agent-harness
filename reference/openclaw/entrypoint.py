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
shutil.copytree(fixture, workspace, dirs_exist_ok=True, symlinks=True)
proc = subprocess.run(["openclaw", "agent", "--local", "--agent", "main", "--session-id", "bakeoff",
                       "--message", sys.argv[1], "--json"], capture_output=True, text=True)
sys.stderr.write(proc.stderr)
def remove_stale(source, target):
    for child in target.iterdir():
        original = source / child.name
        if not os.path.lexists(original) or child.is_dir() != original.is_dir() or child.is_symlink() != original.is_symlink():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        elif child.is_dir() and not child.is_symlink():
            remove_stale(original, child)


remove_stale(workspace, fixture)
shutil.copytree(workspace, fixture, dirs_exist_ok=True, symlinks=True)
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

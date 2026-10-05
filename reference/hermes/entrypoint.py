"""Resolve benchmark-owned env references, then exec the native headless CLI."""
import json
import os
from pathlib import Path
import sys
import sqlite3
import subprocess

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
proc = subprocess.run(["hermes", "chat", "--provider", "custom", "--model", os.environ["LLM_MODEL"],
                       "--format", "stream-json", *sys.argv[1:]], text=True, capture_output=True)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
# Read the candidate's native accounting after it closes its writers. This
# includes compression/title tokens without treating auxiliary calls as turns.
db_path = home / "state.db"
if db_path.is_file():
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            usage = list(db.execute("SELECT task, api_call_count, input_tokens, output_tokens FROM session_model_usage"))
            turns = sum(r["api_call_count"] for r in usage if not r["task"])
            compactions = db.execute("SELECT COUNT(*) FROM sessions WHERE end_reason = 'compression'").fetchone()[0]
            failures = db.execute("SELECT COUNT(*) FROM sessions WHERE compression_failure_error IS NOT NULL").fetchone()[0]
            print(json.dumps({"type": "bakeoff_metrics", "turns": turns, "compactions": compactions,
                              "compaction_failures": failures,
                              "prompt_tokens": sum(r["input_tokens"] for r in usage),
                              "completion_tokens": sum(r["output_tokens"] for r in usage)}))
    except sqlite3.Error as error:
        print(f"bakeoff telemetry unavailable: {error}", file=sys.stderr)
sys.exit(proc.returncode)

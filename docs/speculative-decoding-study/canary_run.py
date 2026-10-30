"""#772: the canary's six hard tasks through bakeoff.run.run_model, against the Docker llama-server (serve.sh).

    python docs/speculative-decoding-study/canary_run.py OUT_DIR CONFIG[,CONFIG...] [--repeats 2] [--tasks a,b]

CONFIG names come from spec_bench.CONFIGS. bakeoff.canary itself goes through the production Manager and the
configured default_model, so it can't target this server; run_model is the bake-off path the #174 study used.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402

from bakeoff import run as bakeoff_run  # noqa: E402
from bakeoff.server import LlamaServer  # noqa: E402
from bakeoff.tasks_hard import HARD_TASKS  # noqa: E402
from spec_bench import CONFIGS, SERVE  # noqa: E402

CANARY = HERE.parents[1] / "bakeoff" / "canary.yaml"
SAMPLING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}


class DockerLlamaServer(LlamaServer):
    def command(self) -> list[str]:
        return [str(SERVE), "canary", str(self.log_path.with_suffix(".docker.log")), *self.profile["args"]]

    def stop(self) -> None:
        subprocess.run(["docker", "stop", "ah772-canary"], capture_output=True)
        super().stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("configs")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--tasks", default="")
    a = ap.parse_args()
    ids = a.tasks.split(",") if a.tasks else yaml.safe_load(CANARY.read_text())["hard"]
    tasks = [t for t in HARD_TASKS if t.id in ids]
    names = a.configs.split(",")
    config = {"port": 8081, "ctx_size": 32768, "common_args": [], "llama_server": str(SERVE),
              "models": {n: {"args": CONFIGS[n], "sampling": SAMPLING} for n in names}}
    bakeoff_run.LlamaServer = DockerLlamaServer
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summaries = []
    for n in names:
        summaries.append(bakeoff_run.run_model(n, config, tasks, a.repeats, out, skip_perf=True))
        (out / "summaries.json").write_text(json.dumps(summaries, indent=2))
        bakeoff_run.write_report(summaries, out)
    print(f"report: {out / 'summary.md'}")


if __name__ == "__main__":
    main()

"""Bake-off tasks. Each task has fixture files, a prompt, a checker, and a reference
solution used by `--selftest` to prove the checker passes when the work is right
and fails when nothing was done."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from .sandbox import Sandbox

PYTEST = "python -m pytest -q -p no:cacheprovider"
PYTEST_INI = "[pytest]\npythonpath = .\ntestpaths = tests\n"


@dataclass
class Context:
    ws: Path
    sandbox: Sandbox
    answer: str
    baseline: dict[str, str]  # file hashes right after setup

    def __post_init__(self) -> None:
        # Models love typographic dashes and narrow spaces ("plex‑webhook"); grade the content, not the glyphs.
        self.answer = re.sub(r"[‐-―−]", "-", self.answer)
        self.answer = re.sub(r"[   ]", " ", self.answer)


@dataclass
class Task:
    id: str
    category: str
    prompt: str
    files: Callable[[], dict[str, str]]
    check: Callable[[Context], tuple[bool, str]]
    solve: Callable[[Context], str]
    setup: Callable[[Sandbox], None] | None = None


def materialize(ws: Path, files: dict[str, str]) -> None:
    # Fixtures with tests get a normal pytest config, so bare `pytest` works like in a real repo.
    if any(rel.startswith("tests/") for rel in files) or "textutil/slug.py" in files or "bank/account.py" in files:
        files = {"pytest.ini": PYTEST_INI, **files}
    for rel, content in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)


def hash_tree(ws: Path) -> dict[str, str]:
    skip = {".git", "__pycache__", ".pytest_cache"}
    return {
        p.relative_to(ws).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(ws.rglob("*"))
        if p.is_file() and not skip.intersection(p.relative_to(ws).parts)
    }


def unchanged(ctx: Context, rel: str) -> bool:
    p = ctx.ws / rel
    return p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == ctx.baseline.get(rel)


def write(ctx: Context, rel: str, content: str) -> None:
    materialize(ctx.ws, {rel: content})


def pytest_ok(ctx: Context, target: str = "") -> tuple[bool, str]:
    code, out = ctx.sandbox.exec(f"{PYTEST} {target}".strip(), timeout=120)
    return code == 0, out.strip().splitlines()[-1] if out.strip() else f"exit {code}"


def has_all(text: str, *needles: str) -> bool:
    low = text.lower()
    return all(n.lower() in low for n in needles)


# --- 1. repo_qa -------------------------------------------------------------

def repo_qa_files() -> dict[str, str]:
    return {
        "README.md": "# tinyapp\n\nA small HTTP service. Configuration lives in the `app` package.\n",
        "app/__init__.py": "",
        "app/config.py": (
            "import os\n\n"
            "DEFAULT_HOST = os.environ.get(\"APP_HOST\", \"0.0.0.0\")\n"
            "DEFAULT_PORT = int(os.environ.get(\"APP_PORT\", \"8731\"))\n"
            "LOG_LEVEL = os.environ.get(\"APP_LOG_LEVEL\", \"info\")\n"
        ),
        "app/server.py": (
            "from http.server import HTTPServer, BaseHTTPRequestHandler\n\n"
            "from app import config\n\n\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "        self.wfile.write(b\"ok\")\n\n\n"
            "def serve():\n"
            "    HTTPServer((config.DEFAULT_HOST, config.DEFAULT_PORT), Handler).serve_forever()\n"
        ),
        "docs/history.md": "## 2024\n\nThe service originally listened on port 5000 before the config refactor.\n",
    }


def repo_qa_check(ctx: Context) -> tuple[bool, str]:
    ok = "8731" in ctx.answer and "config.py" in ctx.answer
    return ok, "needs 8731 and app/config.py"


# --- 2. fix_failing_test ----------------------------------------------------

STATS_PY = '''def mean(values):
    if not values:
        raise ValueError("mean() of empty sequence")
    return sum(values) / len(values)


def median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid] + s[mid + 1]) / 2
'''

STATS_TESTS = '''import pytest

from calc.stats import mean, median


def test_mean():
    assert mean([1, 2, 3, 4]) == 2.5


def test_median_odd():
    assert median([5, 1, 3]) == 3


def test_median_even():
    assert median([4, 1, 3, 2]) == 2.5


def test_median_empty_raises():
    with pytest.raises(ValueError):
        median([])
'''


def fix_test_check(ctx: Context) -> tuple[bool, str]:
    if not unchanged(ctx, "tests/test_stats.py"):
        return False, "tests were modified"
    return pytest_ok(ctx)


def fix_test_solve(ctx: Context) -> str:
    fixed = STATS_PY.replace(
        "    s = sorted(values)\n",
        "    if not values:\n        raise ValueError(\"median() of empty sequence\")\n    s = sorted(values)\n",
    ).replace("(s[mid] + s[mid + 1]) / 2", "(s[mid - 1] + s[mid]) / 2")
    write(ctx, "calc/stats.py", fixed)
    return "fixed median"


# --- 3. implement_feature ---------------------------------------------------

SLUG_STUB = '''def slugify(text: str, max_length: int = 50) -> str:
    """Convert text to a URL slug.

    Rules:
    - Lowercase the text first.
    - Keep ASCII letters a-z and digits 0-9. Any run of other characters
      (spaces, punctuation, non-ASCII letters) becomes a single hyphen.
    - No leading or trailing hyphens.
    - Truncate to at most max_length characters, then strip any trailing hyphen.
    """
    raise NotImplementedError
'''

SLUG_HIDDEN_TESTS = '''from textutil.slug import slugify


def test_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_collapses_and_strips():
    assert slugify("  --Already--slugged--  ") == "already-slugged"


def test_non_ascii_becomes_hyphen():
    assert slugify("Caf\\u00e9 au lait") == "caf-au-lait"


def test_truncate_then_strip():
    assert slugify("aaaaaaaaaa b", max_length=11) == "aaaaaaaaaa"


def test_empty():
    assert slugify("") == ""


def test_digits_kept():
    assert slugify("Release 2026.09 Notes") == "release-2026-09-notes"
'''


def slug_check(ctx: Context) -> tuple[bool, str]:
    write(ctx, "tests/_hidden_test_slug.py", SLUG_HIDDEN_TESTS)
    return pytest_ok(ctx, "tests/_hidden_test_slug.py")


def slug_solve(ctx: Context) -> str:
    write(ctx, "textutil/slug.py", (
        "import re\n\n\n"
        "def slugify(text: str, max_length: int = 50) -> str:\n"
        "    s = re.sub(r\"[^a-z0-9]+\", \"-\", text.lower()).strip(\"-\")\n"
        "    return s[:max_length].rstrip(\"-\")\n"
    ))
    return "implemented slugify"


# --- 4. rename_across_files -------------------------------------------------

def rename_files() -> dict[str, str]:
    return {
        "shop/__init__.py": "",
        "shop/pricing.py": (
            "def calc_total(items, tax_rate=0.0):\n"
            "    \"\"\"Return the order total for (price, qty) pairs including tax, rounded to cents.\"\"\"\n"
            "    subtotal = sum(price * qty for price, qty in items)\n"
            "    return round(subtotal * (1 + tax_rate), 2)\n"
        ),
        "shop/cart.py": (
            "from shop.pricing import calc_total\n\n\n"
            "class Cart:\n"
            "    def __init__(self, tax_rate=0.0):\n"
            "        self.tax_rate = tax_rate\n"
            "        self.items = []\n\n"
            "    def add(self, price, qty=1):\n"
            "        self.items.append((price, qty))\n\n"
            "    def total(self):\n"
            "        return calc_total(self.items, self.tax_rate)\n"
        ),
        "shop/report.py": (
            "from shop import pricing\n\n\n"
            "def summary_line(order_id, items, tax_rate):\n"
            "    total = pricing.calc_total(items, tax_rate)\n"
            "    return f\"Order {order_id}: ${total:.2f}\"\n"
        ),
        "tests/test_shop.py": (
            "from shop.cart import Cart\n"
            "from shop.pricing import calc_total\n"
            "from shop.report import summary_line\n\n\n"
            "def test_calc_total_with_tax():\n"
            "    assert calc_total([(10.0, 2), (5.0, 1)], 0.1) == 27.5\n\n\n"
            "def test_cart_total():\n"
            "    cart = Cart()\n"
            "    cart.add(3.25, 4)\n"
            "    assert cart.total() == 13.0\n\n\n"
            "def test_summary_line():\n"
            "    assert summary_line(42, [(19.99, 1)], 0.0) == \"Order 42: $19.99\"\n"
        ),
    }


def rename_check(ctx: Context) -> tuple[bool, str]:
    leftovers = [p.relative_to(ctx.ws).as_posix() for p in ctx.ws.rglob("*.py")
                 if "calc_total" in p.read_text(encoding="utf-8")]
    if leftovers:
        return False, f"calc_total still in {leftovers}"
    if "def compute_order_total" not in (ctx.ws / "shop/pricing.py").read_text(encoding="utf-8"):
        return False, "definition not renamed"
    return pytest_ok(ctx)


def rename_solve(ctx: Context) -> str:
    for p in ctx.ws.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "calc_total" in text:
            write(ctx, p.relative_to(ctx.ws).as_posix(), text.replace("calc_total", "compute_order_total"))
    return "renamed"


# --- 5. data_crunch ---------------------------------------------------------

def sales_rows() -> list[tuple[str, str, int, float]]:
    rng = random.Random(7)
    regions = ["north", "south", "east", "west"]
    products = ["widget", "gadget", "doohickey", "sprocket"]
    prices = [2.5, 4.99, 12.0, 19.95]
    return [(rng.choice(regions), rng.choice(products), rng.randint(1, 20), rng.choice(prices)) for _ in range(240)]


def sales_files() -> dict[str, str]:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["region", "product", "units", "unit_price"])
    w.writerows(sales_rows())
    return {"data/sales.csv": buf.getvalue()}


def sales_expected() -> dict[str, float]:
    totals: dict[str, float] = {}
    for region, _, units, price in sales_rows():
        totals[region] = totals.get(region, 0.0) + units * price
    return {k: round(v, 2) for k, v in totals.items()}


def sales_check(ctx: Context) -> tuple[bool, str]:
    out = ctx.ws / "out/revenue.json"
    if not out.is_file():
        return False, "out/revenue.json missing"
    try:
        got = json.loads(out.read_text(encoding="utf-8"))
    except ValueError:
        return False, "invalid JSON"
    expected = sales_expected()
    if not isinstance(got, dict) or set(got) != set(expected):
        return False, f"regions {sorted(got) if isinstance(got, dict) else got}"
    bad = {k: got[k] for k in expected if not isinstance(got[k], (int, float)) or abs(got[k] - expected[k]) > 0.011}
    return not bad, f"wrong values {bad}" if bad else "ok"


def sales_solve(ctx: Context) -> str:
    write(ctx, "out/revenue.json", json.dumps(sales_expected()))
    return "done"


# --- 6. recover_from_error --------------------------------------------------

BUILD_PY = '''import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    with open(ROOT / "config" / "build.json") as f:
        config = json.load(f)
    src = ROOT / config["source_dir"]
    out = ROOT / config["output_dir"]
    names = sorted(p.relative_to(src).as_posix() for p in src.rglob("*.py"))
    if not names:
        sys.exit(f"error: no source files found in {src}")
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.txt").write_text("\\n".join(names) + "\\n")
    print(f"wrote {len(names)} entries to {out / 'manifest.txt'}")


if __name__ == "__main__":
    main()
'''


def build_files() -> dict[str, str]:
    return {
        "scripts/build.py": BUILD_PY,
        "config/build.json": '{\n  "source_dir": "src",\n  "output_dir": "dist",\n}\n',
        "lib/core.py": "def run():\n    return 1\n",
        "lib/util/helpers.py": "def helper():\n    return 2\n",
    }


def build_check(ctx: Context) -> tuple[bool, str]:
    if not unchanged(ctx, "scripts/build.py"):
        return False, "build.py was modified"
    manifest = ctx.ws / "dist/manifest.txt"
    if not manifest.is_file():
        return False, "dist/manifest.txt missing"
    lines = manifest.read_text(encoding="utf-8").split()
    return lines == ["core.py", "util/helpers.py"], f"manifest {lines}"


def build_solve(ctx: Context) -> str:
    write(ctx, "config/build.json", '{"source_dir": "lib", "output_dir": "dist"}\n')
    ctx.sandbox.exec("python scripts/build.py")
    return "fixed config"


# --- 7. log_diagnosis -------------------------------------------------------

def log_files() -> dict[str, str]:
    rng = random.Random(11)

    def ts(sec: int) -> str:
        return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

    grafana, prom, cadvisor, plex = [], [], [], []
    for sec in range(0, 6 * 3600, 60):
        t = ts(sec + rng.randint(0, 50))
        grafana.append(f"logger=cleanup t=2026-09-13T{t}Z level=info msg=\"Completed cleanup jobs\" duration={rng.randint(3, 40)}ms")
        if sec % 180 == 0:
            cadvisor.append(f"I0913 {t}.{rng.randint(100, 999)} manager.go:1234] Housekeeping took {rng.randint(80, 400)}ms")
        crashed = sec > 3 * 3600 + 14 * 60
        target = "plex-webhook:8000" if crashed and sec % 120 == 0 else rng.choice(["cadvisor:8080", "host.docker.internal:9182"])
        if crashed and sec % 120 == 0:
            prom.append(f"ts=2026-09-13T{t}Z level=warn component=\"scrape manager\" msg=\"Scrape failed\" target={target} err=\"connection refused\"")
        else:
            prom.append(f"ts=2026-09-13T{t}Z level=info component=tsdb msg=\"Head GC completed\" target={target} duration={rng.randint(1, 9)}ms")
        if sec < 3 * 3600 + 14 * 60:
            plex.append(f"2026-09-13 {t},{rng.randint(100, 999)} INFO app.main: POST /webhook 200 event=media.{rng.choice(['play', 'pause', 'resume', 'stop'])}")
    grafana.insert(121, "logger=alerting.notifier t=2026-09-13T02:01:33Z level=error msg=\"Failed to send alert notification\" error=\"context deadline exceeded\"")
    plex += [
        "2026-09-13 03:14:05,101 ERROR app.store: failed to insert event",
        "Traceback (most recent call last):",
        "  File \"/app/app/store.py\", line 58, in insert_event",
        "    conn.execute(INSERT_SQL, row)",
        "sqlite3.OperationalError: database is locked",
        "2026-09-13 03:14:06,877 ERROR uvicorn.error: Exception in ASGI application",
        "2026-09-13 03:14:07,512 CRITICAL uvicorn: worker process exited with code 1",
    ]
    return {
        "logs/grafana.log": "\n".join(grafana) + "\n",
        "logs/prometheus.log": "\n".join(prom) + "\n",
        "logs/cadvisor.log": "\n".join(cadvisor) + "\n",
        "logs/plex-webhook.log": "\n".join(plex) + "\n",
    }


def log_check(ctx: Context) -> tuple[bool, str]:
    return has_all(ctx.answer, "plex-webhook", "03:14:07", "database is locked"), "needs service, exit time, root cause"


# --- 8. yaml_config_edit ----------------------------------------------------

ROOMS_YAML = """rooms:
  living_room:
    clients:
      - title: Living Room Apple TV
        uuid: a1b2c3-living
    lights:
      - govee-living-strip
      - govee-living-lamp
  bedroom:
    clients:
      - title: Bedroom Google TV
        uuid: d4e5f6-bedroom
    lights:
      - tuya-bedroom-bulb
dispatcher:
  dim_level: 20
  restore_level: 100
"""


def yaml_check(ctx: Context) -> tuple[bool, str]:
    try:
        data = yaml.safe_load((ctx.ws / "config/rooms.yaml").read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        return False, f"unreadable yaml: {e}"
    original = yaml.safe_load(ROOMS_YAML)
    expected_office = {"clients": [{"title": "Office Mac", "uuid": "c0ffee-01"}], "lights": ["govee-desk", "govee-shelf"]}
    if not isinstance(data, dict) or data.get("dispatcher") != original["dispatcher"]:
        return False, "dispatcher changed"
    rooms = data.get("rooms") or {}
    for name in ("living_room", "bedroom"):
        if rooms.get(name) != original["rooms"][name]:
            return False, f"{name} changed"
    return rooms.get("office") == expected_office, f"office={rooms.get('office')}"


def yaml_solve(ctx: Context) -> str:
    data = yaml.safe_load(ROOMS_YAML)
    data["rooms"]["office"] = {"clients": [{"title": "Office Mac", "uuid": "c0ffee-01"}], "lights": ["govee-desk", "govee-shelf"]}
    write(ctx, "config/rooms.yaml", yaml.safe_dump(data, sort_keys=False))
    return "added office"


# --- 9. needle_search -------------------------------------------------------

def needle_files() -> dict[str, str]:
    rng = random.Random(3)
    names = ["TIMEOUT", "MAX_ITEMS", "POOL_SIZE", "CACHE_SECONDS", "BATCH_SIZE", "RETRIES", "PAGE_LIMIT"]
    files = {"pkg/__init__.py": ""}
    for i in range(60):
        lines = [f"\"\"\"Module {i} of the sync pipeline.\"\"\"", ""]
        for n in rng.sample(names, 3):
            lines.append(f"{n}_{i} = {rng.randint(1, 500)}")
        lines += ["", f"def step_{i}(payload):", f"    return {{**payload, \"step\": {i}}}"]
        files[f"pkg/mod_{i:02d}.py"] = "\n".join(lines) + "\n"
    files["pkg/mod_12.py"] += "\n# NOTE: RETRY_BACKOFF_SECONDS used to be defined here (it was 5) before the refactor.\n"
    files["pkg/mod_37.py"] += "\nRETRY_BACKOFF_SECONDS = 17\n"
    files["pkg/mod_50.py"] = "from pkg.mod_37 import RETRY_BACKOFF_SECONDS\n\n" + files["pkg/mod_50.py"]
    return files


def needle_check(ctx: Context) -> tuple[bool, str]:
    return bool(re.search(r"\b17\b", ctx.answer)) and "mod_37" in ctx.answer, "needs 17 and pkg/mod_37.py"


# --- 10. write_tests (mutation-checked) -------------------------------------

ACCOUNT_PY = '''class InsufficientFunds(Exception):
    pass


class Account:
    def __init__(self, owner, balance=0):
        self.owner = owner
        self._balance = balance

    @property
    def balance(self):
        return self._balance

    def deposit(self, amount):
        if amount <= 0:
            raise ValueError("deposit amount must be positive")
        self._balance += amount
        return self._balance

    def withdraw(self, amount):
        if amount <= 0:
            raise ValueError("withdrawal amount must be positive")
        if amount > self._balance:
            raise InsufficientFunds(f"cannot withdraw {amount}; balance is {self._balance}")
        self._balance -= amount
        return self._balance
'''

MUTANTS = {
    "overdraft_allowed": ("if amount > self._balance:", "if False:"),
    "deposit_unchecked": (
        '        if amount <= 0:\n            raise ValueError("deposit',
        '        if False:\n            raise ValueError("deposit',
    ),
    "withdraw_adds": ("self._balance -= amount", "self._balance += amount"),
}


def write_tests_check(ctx: Context) -> tuple[bool, str]:
    if not (ctx.ws / "tests/test_account.py").is_file():
        return False, "tests/test_account.py missing"
    if not unchanged(ctx, "bank/account.py"):
        return False, "account.py was modified"
    ok, summary = pytest_ok(ctx, "tests/test_account.py")
    if not ok:
        return False, f"tests fail on correct code: {summary}"
    killed = []
    try:
        for name, (old, new) in MUTANTS.items():
            write(ctx, "bank/account.py", ACCOUNT_PY.replace(old, new, 1))
            if not pytest_ok(ctx, "tests/test_account.py")[0]:
                killed.append(name)
    finally:
        write(ctx, "bank/account.py", ACCOUNT_PY)
    return len(killed) == len(MUTANTS), f"killed {len(killed)}/{len(MUTANTS)} mutants {killed}"


def write_tests_solve(ctx: Context) -> str:
    write(ctx, "tests/test_account.py", (
        "import pytest\n\nfrom bank.account import Account, InsufficientFunds\n\n\n"
        "def test_deposit_and_withdraw():\n"
        "    a = Account(\"d\", 10)\n"
        "    assert a.deposit(5) == 15\n"
        "    assert a.withdraw(4) == 11\n"
        "    assert a.balance == 11\n\n\n"
        "def test_overdraft():\n"
        "    with pytest.raises(InsufficientFunds):\n"
        "        Account(\"d\", 1).withdraw(2)\n\n\n"
        "def test_bad_deposit():\n"
        "    with pytest.raises(ValueError):\n"
        "        Account(\"d\").deposit(0)\n"
    ))
    return "wrote tests"


# --- 11. read_only_count ----------------------------------------------------

def todo_files() -> dict[str, str]:
    return {
        "app/__init__.py": "",
        "app/main.py": (
            "# TODO: read settings from the environment\n"
            "import sys\n\n\n"
            "def main():\n"
            "    # TODO: add argument parsing\n"
            "    print(\"hello\", file=sys.stdout)\n"
        ),
        "app/db.py": (
            "def connect():\n"
            "    # TODO: connection pooling\n"
            "    pass\n\n\n"
            "def migrate():\n"
            "    # TODO: run migrations in a transaction\n"
            "    # TODO: log applied migration ids\n"
            "    pass\n"
        ),
        "app/util.py": "def chunk(xs, n):\n    # TODO: validate n > 0\n    return [xs[i:i + n] for i in range(0, len(xs), n)]\n",
        "tests/test_main.py": "# TODO: real tests\ndef test_placeholder():\n    assert True\n",
        "NOTES.md": "- TODO: write docs\n- TODO: pick a license\n",
        "app/legacy.txt": "TODO: delete this file\n",
    }


def todo_check(ctx: Context) -> tuple[bool, str]:
    if hash_tree(ctx.ws) != ctx.baseline:
        return False, "files were modified"
    numbers = re.findall(r"\b\d+\b", ctx.answer)
    return "7" in numbers and not {"8", "9", "10"}.intersection(numbers), f"answer numbers {numbers}"


# --- 12. long_context_detail ------------------------------------------------

def design_doc() -> dict[str, str]:
    rng = random.Random(5)
    subjects = ["The ingest worker", "The API gateway", "The scheduler", "The metrics exporter", "The session store",
                "The notification service", "The sandbox manager", "The model router"]
    verbs = ["batches", "retries", "validates", "compresses", "shards", "deduplicates", "streams", "caches"]
    objects = ["incoming events", "tool call results", "audit records", "transcript chunks", "queue entries",
               "health probes", "configuration snapshots", "approval requests"]
    reasons = ["to keep tail latency predictable", "so restarts do not lose work", "because the GPU is shared",
               "to stay within the 16 GB VRAM budget", "so the phone client can resume a stream",
               "to simplify operational debugging"]
    sections = []
    for i in range(1, 41):
        paras = []
        for _ in range(3):
            sentences = [f"{rng.choice(subjects)} {rng.choice(verbs)} {rng.choice(objects)} {rng.choice(reasons)}."
                         for _ in range(rng.randint(5, 8))]
            paras.append(" ".join(sentences))
        if i == 9:
            paras.append("The asset cache TTL is 12 hours, since static assets rarely change between deploys.")
        if i == 23:
            paras.append("The session cache TTL is 47 minutes. We picked 47 rather than a round hour because cached "
                         "sessions must expire before the identity provider's 60-minute token refresh, leaving "
                         "margin for clock skew between hosts.")
        sections.append(f"## {i}. Component notes\n\n" + "\n\n".join(paras))
    return {"docs/design.md": "# Platform design notes\n\n" + "\n\n".join(sections) + "\n"}


def design_check(ctx: Context) -> tuple[bool, str]:
    return bool(re.search(r"\b47\b", ctx.answer)) and "refresh" in ctx.answer.lower(), "needs 47 minutes + token refresh reason"


# --- 13. git_branch_commit --------------------------------------------------

def git_setup(sandbox: Sandbox) -> None:
    code, out = sandbox.exec("git init -q -b main && git add -A && git commit -qm 'Initial commit'")
    if code != 0:
        raise RuntimeError(f"git setup failed: {out}")


def git_check(ctx: Context) -> tuple[bool, str]:
    sb = ctx.sandbox
    code, subject = sb.exec("git log -1 --format=%s feature/greeting")
    if code != 0:
        return False, "branch feature/greeting missing"
    _, branch_file = sb.exec("git show feature/greeting:hello.py")
    _, main_file = sb.exec("git show main:hello.py")
    if subject.strip() != "Update greeting":
        return False, f"commit subject {subject.strip()!r}"
    if "Hello, harness!" not in branch_file:
        return False, "greeting not changed on branch"
    return "Hello, world!" in main_file, "main was changed" if "Hello, world!" not in main_file else "ok"


def git_solve(ctx: Context) -> str:
    ctx.sandbox.exec("git checkout -q -b feature/greeting")
    write(ctx, "hello.py", 'def greeting():\n    return "Hello, harness!"\n')
    ctx.sandbox.exec("git commit -qam 'Update greeting'")
    return "committed"


TASKS: list[Task] = [
    Task("repo_qa", "understand",
         "What TCP port does this app listen on by default, and which file defines that default? "
         "Answer with the port number and the file path.",
         repo_qa_files, repo_qa_check, lambda ctx: "Port 8731, defined in app/config.py"),
    Task("fix_failing_test", "debug",
         "The test suite is failing. Fix the code (do not modify the tests) so `python -m pytest` passes.",
         lambda: {"calc/__init__.py": "", "calc/stats.py": STATS_PY, "tests/test_stats.py": STATS_TESTS},
         fix_test_check, fix_test_solve),
    Task("implement_feature", "code",
         "Implement `slugify` in textutil/slug.py exactly as its docstring describes. "
         "Write a few tests for it and run them to check your work.",
         lambda: {"textutil/__init__.py": "", "textutil/slug.py": SLUG_STUB},
         slug_check, slug_solve),
    Task("rename_across_files", "refactor",
         "Rename the function `calc_total` to `compute_order_total` everywhere in the Python code: "
         "the definition, every call site, and the tests. The tests must still pass afterwards.",
         rename_files, rename_check, rename_solve),
    Task("data_crunch", "shell",
         "Using data/sales.csv, compute the total revenue (units * unit_price) for each region, rounded to 2 decimal "
         "places, and write it to out/revenue.json as a JSON object mapping region name to revenue.",
         sales_files, sales_check, sales_solve),
    Task("recover_from_error", "debug",
         "Run `python scripts/build.py` and make sure it succeeds and writes its manifest. "
         "Fix whatever is broken, but do not modify scripts/build.py.",
         build_files, build_check, build_solve),
    Task("log_diagnosis", "homelab",
         "One service in this homelab crashed overnight and never came back. Using only the logs in logs/, "
         "tell me which service crashed, the exact time (HH:MM:SS) its process exited, and the root-cause error.",
         log_files, log_check, lambda ctx: "plex-webhook exited at 03:14:07: sqlite3.OperationalError: database is locked"),
    Task("yaml_config_edit", "homelab",
         "In config/rooms.yaml, add a new room named `office` with one client titled `Office Mac` (uuid `c0ffee-01`) "
         "and the lights `govee-desk` and `govee-shelf`, following the same structure as the existing rooms. "
         "Do not change anything else.",
         lambda: {"config/rooms.yaml": ROOMS_YAML}, yaml_check, yaml_solve),
    Task("needle_search", "understand",
         "What value is RETRY_BACKOFF_SECONDS currently set to, and in which file is it defined?",
         needle_files, needle_check, lambda ctx: "17, defined in pkg/mod_37.py"),
    Task("write_tests", "code",
         "Write a thorough pytest file at tests/test_account.py for bank/account.py. Cover normal behavior and "
         "every error case. All of your tests must pass against the current code. Do not modify bank/account.py.",
         lambda: {"bank/__init__.py": "", "bank/account.py": ACCOUNT_PY},
         write_tests_check, write_tests_solve),
    Task("read_only_count", "understand",
         "Without modifying any files, count how many TODO comments there are in this repo's Python (.py) files. "
         "Answer with just the number.",
         todo_files, todo_check, lambda ctx: "7"),
    Task("long_context_detail", "understand",
         "Read docs/design.md and tell me the session cache TTL and the reason the document gives for that value.",
         design_doc, design_check,
         lambda ctx: "47 minutes, so sessions expire before the 60-minute token refresh"),
    Task("git_branch_commit", "git",
         "This workspace is a git repository. Create a branch named `feature/greeting`, change the greeting "
         "returned by hello.py to `Hello, harness!`, and commit that change on the branch with the message "
         "`Update greeting`. Leave `main` unchanged.",
         lambda: {"hello.py": 'def greeting():\n    return "Hello, world!"\n'},
         git_check, git_solve, setup=git_setup),
]

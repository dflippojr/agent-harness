"""Harder bake-off tasks.

The core suite (`tasks.py`) topped out at 26/26 for Qwen3.6, so it can't separate a good model or harness from a
great one. These tasks need more steps, more reading of tool output, and more care with details. Most use hidden
checks the agent never sees, so special-casing the visible tests doesn't pass.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3

import yaml

from .sandbox import Sandbox
from .tasks import Context, Task, pytest_ok, unchanged, write

HARD_LIMITS = {"max_turns": 50, "wall_limit": 1800}

# --- 1. multi_bug_inventory -------------------------------------------------
# Three independent bugs in three modules. The visible tests catch each one; hidden tests check the same behavior
# with different numbers.

INV_MODELS = '''from dataclasses import dataclass


@dataclass
class Item:
    sku: str
    name: str
    unit_price_cents: int
    quantity: int

    def value_cents(self) -> int:
        return self.unit_price_cents * self.quantity
'''

INV_STORE = '''from inventory.models import Item


class Store:
    def __init__(self):
        self._items: dict[str, Item] = {}

    def add(self, item: Item) -> None:
        """Add stock. Adding an existing SKU increases its quantity and keeps the newest price."""
        if item.sku in self._items:
            existing = self._items[item.sku]
            existing.quantity = item.quantity
            existing.unit_price_cents = item.unit_price_cents
        else:
            self._items[item.sku] = Item(item.sku, item.name, item.unit_price_cents, item.quantity)

    def remove(self, sku: str, quantity: int) -> None:
        """Remove stock. Raises KeyError for an unknown SKU and ValueError if there isn't enough.
        A SKU whose quantity reaches zero is dropped from the store."""
        item = self._items[sku]
        if quantity > item.quantity:
            raise ValueError(f"only {item.quantity} of {sku} in stock")
        item.quantity -= quantity
        if item.quantity < 0:
            del self._items[sku]

    def get(self, sku: str) -> Item | None:
        return self._items.get(sku)

    def items(self) -> list[Item]:
        return list(self._items.values())
'''

INV_REPORT = '''from inventory.store import Store


def total_value_cents(store: Store) -> int:
    return sum(item.value_cents() for item in store.items())


def low_stock(store: Store, threshold: int) -> list[str]:
    """SKUs with quantity at or below threshold, sorted alphabetically."""
    return sorted(item.sku for item in store.items() if item.quantity < threshold)


def format_cents(cents: int) -> str:
    """Format cents as dollars, e.g. 123456 -> "$1,234.56"."""
    return f"${cents // 100:,}.{cents % 100}"
'''

INV_TESTS = '''import pytest

from inventory.models import Item
from inventory.report import format_cents, low_stock, total_value_cents
from inventory.store import Store


def make_store():
    s = Store()
    s.add(Item("A1", "bolt", 25, 10))
    s.add(Item("B2", "nut", 5, 3))
    return s


def test_add_existing_sku_accumulates():
    s = make_store()
    s.add(Item("A1", "bolt", 30, 5))
    assert s.get("A1").quantity == 15
    assert s.get("A1").unit_price_cents == 30


def test_remove_to_zero_drops_sku():
    s = make_store()
    s.remove("B2", 3)
    assert s.get("B2") is None


def test_remove_too_many():
    with pytest.raises(ValueError):
        make_store().remove("B2", 4)


def test_low_stock_is_inclusive():
    assert low_stock(make_store(), 3) == ["B2"]


def test_total_value():
    assert total_value_cents(make_store()) == 265


def test_format_cents():
    assert format_cents(123456) == "$1,234.56"
    assert format_cents(105) == "$1.05"
'''

INV_HIDDEN = '''import pytest

from inventory.models import Item
from inventory.report import format_cents, low_stock, total_value_cents
from inventory.store import Store


def test_hidden_accumulate_twice():
    s = Store()
    for q in (4, 6, 1):
        s.add(Item("Z9", "gear", 100, q))
    assert s.get("Z9").quantity == 11
    assert total_value_cents(s) == 1100


def test_hidden_partial_remove_keeps_sku():
    s = Store()
    s.add(Item("C3", "cog", 7, 5))
    s.remove("C3", 2)
    assert s.get("C3").quantity == 3
    s.remove("C3", 3)
    assert s.items() == []


def test_hidden_remove_unknown():
    with pytest.raises(KeyError):
        Store().remove("nope", 1)


def test_hidden_low_stock_boundaries():
    s = Store()
    s.add(Item("b", "x", 1, 5))
    s.add(Item("a", "y", 1, 6))
    s.add(Item("c", "z", 1, 4))
    assert low_stock(s, 5) == ["b", "c"]
    assert low_stock(s, 3) == []


def test_hidden_format_cents():
    assert format_cents(0) == "$0.00"
    assert format_cents(9) == "$0.09"
    assert format_cents(100000000) == "$1,000,000.00"
    assert format_cents(4210) == "$42.10"
'''


def inv_files() -> dict[str, str]:
    return {
        "inventory/__init__.py": "", "inventory/models.py": INV_MODELS, "inventory/store.py": INV_STORE,
        "inventory/report.py": INV_REPORT, "tests/test_inventory.py": INV_TESTS,
    }


def inv_check(ctx: Context) -> tuple[bool, str]:
    if not unchanged(ctx, "tests/test_inventory.py"):
        return False, "tests were modified"
    ok, note = pytest_ok(ctx, "tests/test_inventory.py")
    if not ok:
        return False, f"visible tests: {note}"
    write(ctx, "tests/_hidden_test_inventory.py", INV_HIDDEN)
    return pytest_ok(ctx, "tests/_hidden_test_inventory.py")


def inv_solve(ctx: Context) -> str:
    write(ctx, "inventory/store.py", INV_STORE.replace("existing.quantity = item.quantity", "existing.quantity += item.quantity")
          .replace("if item.quantity < 0:", "if item.quantity == 0:"))
    write(ctx, "inventory/report.py", INV_REPORT.replace("item.quantity < threshold", "item.quantity <= threshold")
          .replace("{cents % 100}", "{cents % 100:02d}"))
    return "fixed three bugs"


# --- 2. duration_parser (spec with edge cases) ------------------------------

DURATION_STUB = '''def parse_duration(text: str) -> int:
    """Parse a compact duration like "1h30m" or "2d4h15m10s" into a whole number of seconds.

    Rules:
    - Units are d (86400 s), h (3600 s), m (60 s), s (1 s). Each unit is a positive or zero integer
      followed by its letter, e.g. "90m", "0s".
    - Units must appear in the order d, h, m, s, and each unit at most once. "30m1h" and "1h1h" are invalid.
    - Surrounding whitespace is ignored. Whitespace between units is NOT allowed ("1h 30m" is invalid).
    - Letters are case-insensitive ("1H30M" is fine).
    - Leading zeros are allowed ("05m").
    - Anything else raises ValueError: the empty string, a number without a unit, an unknown unit,
      a sign ("+5m", "-5m"), decimals ("1.5h").
    """
    raise NotImplementedError


def format_duration(seconds: int) -> str:
    """Inverse of parse_duration using the fewest characters: largest units first, zero units omitted.

    format_duration(5400) == "1h30m", format_duration(0) == "0s", format_duration(86401) == "1d1s".
    Negative input raises ValueError.
    """
    raise NotImplementedError
'''

DURATION_HIDDEN = '''import pytest

from timeparse.duration import format_duration, parse_duration


@pytest.mark.parametrize("text,expected", [
    ("1h30m", 5400), ("2d4h15m10s", 2 * 86400 + 4 * 3600 + 15 * 60 + 10), ("90m", 5400), ("0s", 0),
    ("  45s\\t", 45), ("1H30M", 5400), ("05m", 300), ("1d", 86400), ("3h7s", 10807),
])
def test_parse_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "30", "30m1h", "1h1h", "1h 30m", "5x", "+5m", "-5m", "1.5h", "h", "1hm", "m30",
])
def test_parse_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize("seconds,expected", [
    (5400, "1h30m"), (0, "0s"), (86401, "1d1s"), (59, "59s"), (3600, "1h"), (90061, "1d1h1m1s"),
])
def test_format(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_negative():
    with pytest.raises(ValueError):
        format_duration(-1)


def test_round_trip():
    for n in [0, 1, 61, 3599, 3601, 86399, 172800, 1234567]:
        assert parse_duration(format_duration(n)) == n
'''

DURATION_SOLUTION = '''import re

_UNITS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
_PATTERN = re.compile(r"(?:(\\d+)d)?(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?", re.IGNORECASE)


def parse_duration(text: str) -> int:
    s = text.strip()
    match = _PATTERN.fullmatch(s)
    if not s or not match or all(g is None for g in match.groups()):
        raise ValueError(f"invalid duration: {text!r}")
    return sum(int(g) * size for g, (_, size) in zip(match.groups(), _UNITS) if g is not None)


def format_duration(seconds: int) -> str:
    if seconds < 0:
        raise ValueError("negative duration")
    parts = []
    for unit, size in _UNITS:
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n}{unit}")
    return "".join(parts) or "0s"
'''


def duration_check(ctx: Context) -> tuple[bool, str]:
    write(ctx, "tests/_hidden_test_duration.py", DURATION_HIDDEN)
    return pytest_ok(ctx, "tests/_hidden_test_duration.py")


def duration_solve(ctx: Context) -> str:
    write(ctx, "timeparse/duration.py", DURATION_SOLUTION)
    return "implemented"


# --- 3. merge_conflict ------------------------------------------------------

MERGE_BASE = '''def build_url(host, path, port=80):
    return f"http://{host}:{port}/{path}"
'''

MERGE_MAIN = '''def build_url(host, path, port=80, secure=False):
    scheme = "https" if secure else "http"
    return f"{scheme}://{host}:{port}/{path}"
'''

MERGE_FEATURE = '''def build_url(host, path, port=80):
    path = path.lstrip("/")
    if port == 80:
        return f"http://{host}/{path}"
    return f"http://{host}:{port}/{path}"
'''

MERGE_HIDDEN = '''from urls import build_url


def test_hidden_plain():
    assert build_url("example.com", "a/b") == "http://example.com/a/b"


def test_hidden_secure_default_port():
    assert build_url("example.com", "/x", port=443, secure=True) == "https://example.com/x"


def test_hidden_secure_custom_port():
    assert build_url("h", "p", port=8443, secure=True) == "https://h:8443/p"


def test_hidden_insecure_custom_port():
    assert build_url("h", "//p", port=8080) == "http://h:8080/p"


def test_hidden_https_on_80_keeps_port():
    assert build_url("h", "p", port=80, secure=True) == "https://h:80/p"
'''


def merge_setup(sb: Sandbox) -> None:
    fixture = sb.workspace / ".fixture"
    fixture.mkdir()
    for name, content in (("feature.py", MERGE_FEATURE), ("main.py", MERGE_MAIN)):
        with open(fixture / name, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    code, out = sb.exec(
        "set -e; git init -q -b main; git add urls.py; git commit -qm 'Initial URL builder'; "
        "git checkout -q -b feature/clean-urls; cp .fixture/feature.py urls.py; "
        "git commit -qam 'Omit default ports and leading slashes'; "
        "git checkout -q main; cp .fixture/main.py urls.py; git commit -qam 'Add https support'; "
        "rm -rf .fixture"
    )
    if code != 0:
        raise RuntimeError(f"merge setup failed: {out}")


def merge_check(ctx: Context) -> tuple[bool, str]:
    sb = ctx.sandbox
    code, branch = sb.exec("git rev-parse --abbrev-ref HEAD")
    if branch.strip() != "main":
        return False, f"HEAD is {branch.strip()!r}, not main"
    code, parents = sb.exec("git rev-list --parents -n 1 HEAD")
    if len(parents.split()) != 3:
        return False, "HEAD is not a merge commit"
    code, status = sb.exec("git status --porcelain --untracked-files=no")
    if status.strip():
        return False, f"uncommitted changes: {status.strip()[:200]}"
    code, merged = sb.exec("git merge-base --is-ancestor feature/clean-urls HEAD && echo yes")
    if "yes" not in merged:
        return False, "feature/clean-urls not merged"
    text = (ctx.ws / "urls.py").read_text(encoding="utf-8")
    if "<<<<<<<" in text or ">>>>>>>" in text:
        return False, "conflict markers left"
    write(ctx, "_hidden_test_urls.py", MERGE_HIDDEN)
    try:
        return pytest_ok(ctx, "_hidden_test_urls.py")
    finally:
        (ctx.ws / "_hidden_test_urls.py").unlink(missing_ok=True)


def merge_solve(ctx: Context) -> str:
    sb = ctx.sandbox
    sb.exec("git merge -q feature/clean-urls")
    write(ctx, "urls.py", (
        'def build_url(host, path, port=80, secure=False):\n'
        '    scheme = "https" if secure else "http"\n'
        '    path = path.lstrip("/")\n'
        '    if port == (443 if secure else 80):\n'
        '        return f"{scheme}://{host}/{path}"\n'
        '    return f"{scheme}://{host}:{port}/{path}"\n'
    ))
    sb.exec("git add urls.py && git commit -qm 'Merge feature/clean-urls'")
    return "merged"


# --- 4. log_correlation -----------------------------------------------------
# 5xx responses are spread over many users; one user's requests account for most of them. The app log
# only has request IDs, the access log only has user IDs, so the two have to be joined.

def corr_data() -> tuple[dict[str, str], dict]:
    rng = random.Random(2026)
    users = [f"u{n:04d}" for n in range(1, 400)]
    culprit = "u0271"
    access, app = [], []
    errors_by_user: dict[str, int] = {}
    for i in range(12000):
        sec = 8 * 3600 + i * 2 + rng.randint(0, 1)
        t = f"2026-09-12T{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}Z"
        rid = f"{rng.getrandbits(48):012x}"
        user = culprit if rng.random() < 0.03 else rng.choice(users)
        path = rng.choice(["/api/items", "/api/cart", "/api/search", "/api/export", "/api/profile"])
        if user == culprit and rng.random() < 0.6:
            path = "/api/export"
        if user == culprit and path == "/api/export":
            status = 500 if rng.random() < 0.9 else 200
        else:
            status = 500 if rng.random() < 0.004 else rng.choice([200, 200, 200, 200, 304, 404])
        access.append(f'{t} 10.0.{rng.randint(0, 9)}.{rng.randint(2, 250)} user={user} "GET {path}" {status} '
                      f'{rng.randint(80, 9000)}b rid={rid}')
        if status == 500:
            errors_by_user[user] = errors_by_user.get(user, 0) + 1
            if path == "/api/export" and user == culprit:
                app.append(f"{t} ERROR export.worker rid={rid} MemoryError while building CSV (rows=2413877)")
            else:
                app.append(f"{t} ERROR api.handlers rid={rid} upstream timeout after 30000ms")
        elif rng.random() < 0.3:
            app.append(f"{t} INFO api.handlers rid={rid} handled in {rng.randint(3, 900)}ms")
    return ({"logs/access.log": "\n".join(access) + "\n", "logs/app.log": "\n".join(app) + "\n"},
            {"culprit": culprit, "count": errors_by_user[culprit], "total": sum(errors_by_user.values())})


def corr_check(ctx: Context) -> tuple[bool, str]:
    facts = corr_data()[1]
    numbers = set(re.findall(r"\b\d+\b", ctx.answer))
    ok = facts["culprit"] in ctx.answer and str(facts["count"]) in numbers and "memoryerror" in ctx.answer.lower()
    return ok, f"needs {facts['culprit']}, {facts['count']} errors, MemoryError"


# --- 5. sqlite_report -------------------------------------------------------

SQL_SCHEMA = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
                     placed_at TEXT NOT NULL, status TEXT NOT NULL, total_cents INTEGER NOT NULL);
CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                      amount_cents INTEGER NOT NULL, refunded_at TEXT NOT NULL);
"""


def sql_rows():
    rng = random.Random(99)
    names = ["Avery", "Blake", "Casey", "Devon", "Emerson", "Finley", "Harper", "Jordan", "Kendall", "Logan",
             "Morgan", "Parker", "Quinn", "Reese", "Rowan", "Sawyer", "Skyler", "Taylor"]
    customers = [(i + 1, n) for i, n in enumerate(names)]
    orders, refunds = [], []
    for oid in range(1, 1501):
        month = rng.randint(1, 9)
        day = rng.randint(1, 28)
        placed = f"2026-{month:02d}-{day:02d} {rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:00"
        if oid % 97 == 0:
            placed = "2026-06-30 23:59:59"
        if oid % 89 == 0:
            placed = "2026-07-01 00:00:00"
        if oid % 83 == 0:
            placed = "2026-04-01 00:00:00"
        status = rng.choices(["paid", "paid", "paid", "cancelled", "pending"], k=1)[0]
        total = rng.randint(500, 60000)
        orders.append((oid, rng.randint(1, len(names)), placed, status, total))
        if status == "paid" and rng.random() < 0.15:
            refunds.append((len(refunds) + 1, oid, rng.randint(100, total), placed))
            if rng.random() < 0.3:
                refunds.append((len(refunds) + 1, oid, rng.randint(1, 100), placed))
    return customers, orders, refunds


def sql_expected() -> list[dict]:
    customers, orders, refunds = sql_rows()
    refunded: dict[int, int] = {}
    for _, oid, amount, _ in refunds:
        refunded[oid] = refunded.get(oid, 0) + amount
    net: dict[int, int] = {}
    for oid, cid, placed, status, total in orders:
        if status == "paid" and "2026-04-01" <= placed[:10] <= "2026-06-30":
            net[cid] = net.get(cid, 0) + total - refunded.get(oid, 0)
    names = dict(customers)
    ranked = sorted(net.items(), key=lambda kv: (-kv[1], names[kv[0]]))[:5]
    return [{"customer": names[cid], "net_revenue_cents": cents} for cid, cents in ranked]


def sql_setup(sb: Sandbox) -> None:
    db = sb.workspace / "data/shop.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    customers, orders, refunds = sql_rows()
    conn = sqlite3.connect(db)
    try:
        conn.executescript(SQL_SCHEMA)
        conn.executemany("INSERT INTO customers VALUES (?, ?)", customers)
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
        conn.executemany("INSERT INTO refunds VALUES (?, ?, ?, ?)", refunds)
        conn.commit()
    finally:
        conn.close()


def sql_check(ctx: Context) -> tuple[bool, str]:
    out = ctx.ws / "out/top_customers.json"
    if not out.is_file():
        return False, "out/top_customers.json missing"
    try:
        got = json.loads(out.read_text(encoding="utf-8"))
    except ValueError:
        return False, "invalid JSON"
    expected = sql_expected()
    return got == expected, "ok" if got == expected else f"got {str(got)[:200]}"


def sql_solve(ctx: Context) -> str:
    write(ctx, "out/top_customers.json", json.dumps(sql_expected(), indent=2))
    return "done"


# --- 6. perf_fix ------------------------------------------------------------

PERF_PY = '''def dedupe_events(events):
    """Remove duplicate events, keeping the first occurrence of each (source, event_id) pair.

    Events are dicts. Order of the surviving events is preserved.
    """
    result = []
    for event in events:
        seen = False
        for kept in result:
            if kept["source"] == event["source"] and kept["event_id"] == event["event_id"]:
                seen = True
                break
        if not seen:
            result.append(event)
    return result


def pairs_with_sum(values, target):
    """Return the number of index pairs (i < j) where values[i] + values[j] == target."""
    count = 0
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            if values[i] + values[j] == target:
                count += 1
    return count
'''

PERF_TESTS = '''from pipeline.events import dedupe_events, pairs_with_sum


def test_dedupe_small():
    events = [{"source": "a", "event_id": 1, "v": 1}, {"source": "b", "event_id": 1, "v": 2},
              {"source": "a", "event_id": 1, "v": 3}]
    assert dedupe_events(events) == events[:2]


def test_pairs_small():
    assert pairs_with_sum([1, 2, 3, 4, 3], 6) == 2
'''

PERF_HIDDEN = '''import random
import time

from pipeline.events import dedupe_events, pairs_with_sum


def reference_pairs(values, target):
    return sum(1 for i in range(len(values)) for j in range(i + 1, len(values)) if values[i] + values[j] == target)


def test_hidden_dedupe_keeps_first_and_order():
    events = [{"source": s, "event_id": e, "n": n} for n, (s, e) in enumerate(
        [("x", 2), ("y", 1), ("x", 2), ("x", 1), ("y", 1), ("z", 9), ("x", 1)])]
    assert [e["n"] for e in dedupe_events(events)] == [0, 1, 3, 5]


def test_hidden_dedupe_unhashable_payload():
    events = [{"source": "s", "event_id": 1, "tags": ["a"]}, {"source": "s", "event_id": 1, "tags": ["b"]}]
    assert dedupe_events(events) == events[:1]


def test_hidden_pairs_duplicates_and_negatives():
    rng = random.Random(4)
    for _ in range(50):
        values = [rng.randint(-5, 5) for _ in range(rng.randint(0, 30))]
        target = rng.randint(-6, 6)
        assert pairs_with_sum(values, target) == reference_pairs(values, target)


def test_hidden_fast_enough():
    rng = random.Random(1)
    events = [{"source": f"s{rng.randint(0, 50)}", "event_id": rng.randint(0, 40000)} for _ in range(120000)]
    values = [rng.randint(0, 1000) for _ in range(120000)]
    started = time.perf_counter()
    kept = dedupe_events(events)
    pairs = pairs_with_sum(values, 1000)
    elapsed = time.perf_counter() - started
    assert len({(e["source"], e["event_id"]) for e in events}) == len(kept)
    assert pairs > 0
    assert elapsed < 3, f"took {elapsed:.1f}s"
'''


def perf_check(ctx: Context) -> tuple[bool, str]:
    if not unchanged(ctx, "tests/test_events.py"):
        return False, "tests were modified"
    write(ctx, "tests/_hidden_test_events.py", PERF_HIDDEN)
    return pytest_ok(ctx, "tests/_hidden_test_events.py")


def perf_solve(ctx: Context) -> str:
    write(ctx, "pipeline/events.py", '''from collections import Counter


def dedupe_events(events):
    seen = set()
    result = []
    for event in events:
        key = (event["source"], event["event_id"])
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result


def pairs_with_sum(values, target):
    counts = Counter()
    total = 0
    for v in values:
        total += counts[target - v]
        counts[v] += 1
    return total
''')
    return "optimized"


# --- 7. compose_diagnosis ---------------------------------------------------

COMPOSE_YML = """services:
  prometheus:
    image: prom/prometheus:v3.5.0
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    ports:
      - "9090:9090"
    networks: [monitoring]

  grafana:
    image: grafana/grafana:12.1.0
    ports:
      - "3000:3000"
    networks: [monitoring]

  node-exporter:
    image: prom/node-exporter:v1.9.1
    command:
      - --web.listen-address=:9110
      - --collector.textfile.directory=/textfile
    networks: [monitoring]

  cadvisor:
    image: gcr.io/cadvisor/cadvisor:v0.52.1
    ports:
      - "8081:8080"
    networks: [monitoring]

  plex-webhook:
    build: ./plex-webhook
    ports:
      - "8765:8000"
    networks: [default]

networks:
  monitoring: {}
"""

PROM_YML = """global:
  scrape_interval: 15s

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]

  - job_name: node
    static_configs:
      # node-exporter's default port
      - targets: ["node-exporter:9100"]

  - job_name: cadvisor
    static_configs:
      - targets: ["cadvisor:8081"]

  - job_name: plex_webhook
    metrics_path: /metrics
    static_configs:
      - targets: ["plex-webhook:8000"]
"""

COMPOSE_NOTES = """# Monitoring stack notes

Dashboards for `node` and `cadvisor` have shown "No data" since the stack was moved into compose, and the
`plex_webhook` panels are empty too. The `prometheus` job itself is fine.
"""


def compose_check(ctx: Context) -> tuple[bool, str]:
    try:
        prom = yaml.safe_load((ctx.ws / "prometheus/prometheus.yml").read_text(encoding="utf-8"))
        compose = yaml.safe_load((ctx.ws / "docker-compose.yml").read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as e:
        return False, f"unreadable yaml: {e}"
    jobs = {j.get("job_name"): j for j in prom.get("scrape_configs") or []}
    if set(jobs) != {"prometheus", "node", "cadvisor", "plex_webhook"}:
        return False, f"jobs {sorted(jobs)}"
    svc = compose.get("services") or {}

    def listen_port(name: str) -> str | None:
        cmd = svc.get(name, {}).get("command") or []
        for arg in cmd:
            if str(arg).startswith("--web.listen-address="):
                return str(arg).rsplit(":", 1)[1]
        return {"node-exporter": "9100", "cadvisor": "8080", "plex-webhook": "8000"}.get(name)

    def targets(job: str) -> list[str]:
        return [t for sc in jobs[job].get("static_configs") or [] for t in sc.get("targets") or []]

    problems = []
    for job, service in (("node", "node-exporter"), ("cadvisor", "cadvisor"), ("plex_webhook", "plex-webhook")):
        want = f"{service}:{listen_port(service)}"
        if targets(job) != [want]:
            problems.append(f"{job} targets {targets(job)} (want {want})")
        nets = svc.get(service, {}).get("networks") or []
        if "monitoring" not in (nets if isinstance(nets, list) else list(nets)):
            problems.append(f"{service} not on monitoring network")
    if targets("prometheus") != ["localhost:9090"]:
        problems.append("prometheus job changed")
    if (svc.get("plex-webhook", {}).get("ports") or []) != ["8765:8000"]:
        problems.append("plex-webhook host port mapping changed")
    return not problems, "; ".join(problems) or "ok"


def compose_solve(ctx: Context) -> str:
    write(ctx, "prometheus/prometheus.yml", PROM_YML.replace("node-exporter:9100", "node-exporter:9110")
          .replace("cadvisor:8081", "cadvisor:8080"))
    write(ctx, "docker-compose.yml", COMPOSE_YML.replace("    networks: [default]\n", "    networks: [default, monitoring]\n"))
    return "fixed"


# --- 8. flaky_shared_state --------------------------------------------------

CART_PY = '''class Cart:
    discounts = []

    def __init__(self, owner, items={}):
        self.owner = owner
        self.items = items

    def add(self, sku, qty=1):
        self.items[sku] = self.items.get(sku, 0) + qty

    def apply_discount(self, code):
        self.discounts.append(code)

    def count(self):
        return sum(self.items.values())
'''

CART_TEST_A = '''from shop.cart import Cart


def test_add_items():
    cart = Cart("alice")
    cart.add("apple", 3)
    assert cart.count() == 3


def test_discount():
    cart = Cart("alice")
    cart.apply_discount("TENOFF")
    assert cart.discounts == ["TENOFF"]
'''

CART_TEST_B = '''from shop.cart import Cart


def test_new_cart_is_empty():
    cart = Cart("bob")
    assert cart.count() == 0
    assert cart.discounts == []


def test_initial_items():
    cart = Cart("bob", {"pear": 1})
    cart.add("pear")
    assert cart.count() == 2
'''

CART_HIDDEN = '''from shop.cart import Cart


def test_hidden_independent_carts():
    a, b = Cart("a"), Cart("b")
    a.add("x", 2)
    a.apply_discount("D1")
    assert b.count() == 0 and b.discounts == []
    assert a.count() == 2 and a.discounts == ["D1"]


def test_hidden_caller_dict_not_mutated():
    start = {"pear": 1}
    Cart("c", start).add("pear")
    assert start == {"pear": 1}
'''


def cart_check(ctx: Context) -> tuple[bool, str]:
    if not (unchanged(ctx, "tests/test_cart_a.py") and unchanged(ctx, "tests/test_cart_b.py")):
        return False, "tests were modified"
    for order in ("tests/test_cart_a.py tests/test_cart_b.py", "tests/test_cart_b.py tests/test_cart_a.py"):
        ok, note = pytest_ok(ctx, order)
        if not ok:
            return False, f"{order}: {note}"
    write(ctx, "tests/_hidden_test_cart.py", CART_HIDDEN)
    return pytest_ok(ctx, "tests/test_cart_a.py tests/_hidden_test_cart.py")


def cart_solve(ctx: Context) -> str:
    write(ctx, "shop/cart.py", '''class Cart:
    def __init__(self, owner, items=None):
        self.owner = owner
        self.items = dict(items or {})
        self.discounts = []

    def add(self, sku, qty=1):
        self.items[sku] = self.items.get(sku, 0) + qty

    def apply_discount(self, code):
        self.discounts.append(code)

    def count(self):
        return sum(self.items.values())
''')
    return "fixed shared state"


# --- 9. cli_json_output -----------------------------------------------------

CLI_TASKS_PY = '''import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class TodoItem:
    id: int
    title: str
    done: bool = False
    tags: tuple[str, ...] = ()


def load(path: Path) -> list[TodoItem]:
    if not path.exists():
        return []
    return [TodoItem(**{**row, "tags": tuple(row.get("tags", ()))}) for row in json.loads(path.read_text())]


def save(path: Path, items: list[TodoItem]) -> None:
    path.write_text(json.dumps([asdict(i) for i in items], indent=2))
'''

CLI_FORMAT_PY = '''from todo.store import TodoItem


def format_table(items: list[TodoItem]) -> str:
    if not items:
        return "no items"
    lines = []
    for item in items:
        mark = "x" if item.done else " "
        tags = f" [{', '.join(item.tags)}]" if item.tags else ""
        lines.append(f"{item.id:>3} [{mark}] {item.title}{tags}")
    return "\\n".join(lines)
'''

CLI_MAIN_PY = '''import argparse
import sys
from pathlib import Path

from todo.format import format_table
from todo.store import TodoItem, load, save


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="todo")
    parser.add_argument("--file", default="todo.json")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("title")
    add.add_argument("--tag", action="append", default=[])

    done = sub.add_parser("done")
    done.add_argument("id", type=int)

    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true", help="include completed items")

    args = parser.parse_args(argv)
    path = Path(args.file)
    items = load(path)

    if args.command == "add":
        item = TodoItem(max((i.id for i in items), default=0) + 1, args.title, tags=tuple(args.tag))
        items.append(item)
        save(path, items)
        print(f"added {item.id}")
    elif args.command == "done":
        for item in items:
            if item.id == args.id:
                item.done = True
                save(path, items)
                print(f"completed {item.id}")
                break
        else:
            print(f"no item {args.id}", file=sys.stderr)
            return 1
    elif args.command == "list":
        shown = items if args.all else [i for i in items if not i.done]
        print(format_table(shown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

CLI_SPEC = """Add a `--format` option to the `list` subcommand of the todo CLI (`python -m todo ...`).

- `--format table` is the default and keeps today's output exactly.
- `--format json` prints a JSON array (and nothing else on stdout) of the shown items, in the same order, where
  each item is an object with exactly the keys `id` (int), `title` (string), `done` (bool) and `tags`
  (array of strings). With no items to show it prints `[]`.
- An unknown format must make argparse exit with status 2.
- `--all` still controls which items are shown, in both formats.

Add tests for the new option under tests/ and make sure the whole test suite passes."""

CLI_HIDDEN = '''import json
import subprocess
import sys


def run(tmp_path, *args):
    return subprocess.run([sys.executable, "-m", "todo", "--file", str(tmp_path / "t.json"), *args],
                          capture_output=True, text=True)


def test_hidden_json_output(tmp_path):
    run(tmp_path, "add", "buy milk", "--tag", "home", "--tag", "errand")
    run(tmp_path, "add", "ship release")
    run(tmp_path, "done", "2")
    out = run(tmp_path, "list", "--format", "json")
    assert out.returncode == 0
    assert json.loads(out.stdout) == [{"id": 1, "title": "buy milk", "done": False, "tags": ["home", "errand"]}]
    everything = json.loads(run(tmp_path, "list", "--all", "--format", "json").stdout)
    assert [i["id"] for i in everything] == [1, 2] and everything[1]["done"] is True and everything[1]["tags"] == []


def test_hidden_json_empty(tmp_path):
    out = run(tmp_path, "list", "--format", "json")
    assert out.returncode == 0 and json.loads(out.stdout) == []


def test_hidden_table_unchanged(tmp_path):
    run(tmp_path, "add", "a", "--tag", "t")
    default = run(tmp_path, "list").stdout
    explicit = run(tmp_path, "list", "--format", "table").stdout
    assert default == explicit == "  1 [ ] a [t]\\n"
    assert run(tmp_path, "list", "--format", "json").stdout.strip().startswith("[")


def test_hidden_bad_format(tmp_path):
    assert run(tmp_path, "list", "--format", "xml").returncode == 2
'''


def cli_files() -> dict[str, str]:
    return {
        "todo/__init__.py": "", "todo/__main__.py": "from todo.cli import main\n\nraise SystemExit(main())\n",
        "todo/store.py": CLI_TASKS_PY, "todo/format.py": CLI_FORMAT_PY, "todo/cli.py": CLI_MAIN_PY,
        "tests/test_format.py": (
            "from todo.format import format_table\nfrom todo.store import TodoItem\n\n\n"
            "def test_table():\n"
            "    assert format_table([TodoItem(1, 'a', True, ('x',))]) == '  1 [x] a [x]'\n"
        ),
    }


def cli_check(ctx: Context) -> tuple[bool, str]:
    new_tests = [p for p in (ctx.ws / "tests").glob("test_*.py") if p.name != "test_format.py"] + (
        [ctx.ws / "tests/test_format.py"] if not unchanged(ctx, "tests/test_format.py") else [])
    if not new_tests:
        return False, "no tests added"
    ok, note = pytest_ok(ctx)
    if not ok:
        return False, f"suite: {note}"
    write(ctx, "tests/_hidden_test_cli.py", CLI_HIDDEN)
    return pytest_ok(ctx, "tests/_hidden_test_cli.py")


def cli_solve(ctx: Context) -> str:
    src = CLI_MAIN_PY.replace(
        '    ls.add_argument("--all", action="store_true", help="include completed items")\n',
        '    ls.add_argument("--all", action="store_true", help="include completed items")\n'
        '    ls.add_argument("--format", choices=["table", "json"], default="table")\n',
    ).replace(
        "        print(format_table(shown))\n",
        "        if args.format == \"json\":\n"
        "            print(json.dumps([{\"id\": i.id, \"title\": i.title, \"done\": i.done, \"tags\": list(i.tags)}"
        " for i in shown]))\n"
        "        else:\n"
        "            print(format_table(shown))\n",
    ).replace("import argparse\n", "import argparse\nimport json\n")
    write(ctx, "todo/cli.py", src)
    write(ctx, "tests/test_cli_json.py", CLI_HIDDEN.replace("test_hidden_", "test_"))
    return "added --format"


# --- 10. trace_config_flow --------------------------------------------------
# Answering needs following an env var through settings -> YAML profile -> per-room override -> clamp.

TRACE_FILES = {
    "lights/__init__.py": "",
    "lights/settings.py": (
        "import os\n\n"
        "PROFILE = os.environ.get(\"LIGHTS_PROFILE\", \"evening\")\n"
        "PROFILE_FILE = \"config/profiles.yaml\"\n"
        "MIN_BRIGHTNESS = 5\n"
        "MAX_BRIGHTNESS = 80\n"
    ),
    "lights/profiles.py": (
        "import yaml\n\n"
        "from lights import settings\n\n\n"
        "def load_profile(name=None):\n"
        "    with open(settings.PROFILE_FILE) as f:\n"
        "        data = yaml.safe_load(f)\n"
        "    profile = dict(data[\"defaults\"])\n"
        "    profile.update(data[\"profiles\"].get(name or settings.PROFILE, {}))\n"
        "    return profile\n"
    ),
    "lights/rules.py": (
        "from lights import settings\n"
        "from lights.profiles import load_profile\n\n\n"
        "def clamp(level):\n"
        "    return max(settings.MIN_BRIGHTNESS, min(settings.MAX_BRIGHTNESS, level))\n\n\n"
        "def target_brightness(event_type, room, profile=None):\n"
        "    profile = profile or load_profile()\n"
        "    overrides = profile.get(\"rooms\", {}).get(room, {})\n"
        "    if event_type in (\"media.play\", \"media.resume\"):\n"
        "        level = overrides.get(\"dim\", profile[\"dim\"])\n"
        "    elif event_type in (\"media.pause\", \"media.stop\"):\n"
        "        level = overrides.get(\"restore\", profile[\"restore\"])\n"
        "        if event_type == \"media.stop\":\n"
        "            level = level + profile.get(\"stop_bonus\", 0)\n"
        "    else:\n"
        "        return None\n"
        "    return clamp(level)\n"
    ),
    "lights/dispatch.py": (
        "from lights.rules import target_brightness\n\n\n"
        "def handle(event):\n"
        "    level = target_brightness(event[\"event\"], event[\"room\"])\n"
        "    if level is None:\n"
        "        return []\n"
        "    return [(light, level) for light in event.get(\"lights\", [])]\n"
    ),
    "config/profiles.yaml": (
        "defaults:\n"
        "  dim: 10\n"
        "  restore: 100\n"
        "  stop_bonus: 0\n\n"
        "profiles:\n"
        "  evening:\n"
        "    dim: 15\n"
        "    restore: 60\n"
        "    stop_bonus: 25\n"
        "    rooms:\n"
        "      bedroom:\n"
        "        restore: 40\n"
        "      living_room:\n"
        "        dim: 3\n"
        "  daytime:\n"
        "    restore: 100\n"
        "    rooms:\n"
        "      living_room:\n"
        "        restore: 90\n"
    ),
    "deploy/lights.env": "# Loaded by the lights service in production\nLIGHTS_PROFILE=daytime\nLOG_LEVEL=info\n",
    "README.md": "# lights\n\nDims room lights when media starts and restores them when it stops.\n",
}

TRACE_PROMPT = (
    "In production (the lights service is started with the environment in deploy/lights.env), a `media.stop` event "
    "arrives for the `living_room`. What brightness level do the living room lights get set to? Then answer the "
    "same question for a `media.stop` event in the `bedroom` when LIGHTS_PROFILE is not set at all. "
    "Explain which settings produced each number."
)


def trace_check(ctx: Context) -> tuple[bool, str]:
    # production: daytime -> living_room restore 90 + stop_bonus 0 (from defaults) -> clamped to MAX 80
    # unset: evening -> bedroom restore 40 + stop_bonus 25 = 65, inside the clamp range
    numbers = re.findall(r"\b\d+\b", ctx.answer)
    return "80" in numbers and "65" in numbers, f"needs 80 and 65, got numbers {numbers[:20]}"


# --- registry ---------------------------------------------------------------

HARD_TASKS: list[Task] = [
    Task("multi_bug_inventory", "debug",
         "The test suite in this repo is failing. Find and fix the bugs in the inventory package so that "
         "`python -m pytest` passes. Do not modify the tests, and fix the real causes rather than special-casing "
         "the test values.",
         inv_files, inv_check, inv_solve),
    Task("duration_parser", "code",
         "Implement `parse_duration` and `format_duration` in timeparse/duration.py exactly as their docstrings "
         "describe, including every error case. Write tests covering the rules and run them.",
         lambda: {"timeparse/__init__.py": "", "timeparse/duration.py": DURATION_STUB, "tests/__init__.py": ""},
         duration_check, duration_solve),
    Task("merge_conflict", "git",
         "This is a git repository. Merge the branch `feature/clean-urls` into `main` and commit the merge on "
         "`main`. The branches conflict in urls.py. Resolve it so the result keeps both changes: https support "
         "from main (the `secure` flag) and the feature branch's behavior (strip leading slashes from the path and "
         "omit the port from the URL when it is the default port for the scheme, which is 80 for http and 443 for "
         "https). Leave no uncommitted changes.",
         lambda: {"urls.py": MERGE_BASE}, merge_check, merge_solve, setup=merge_setup),
    Task("log_correlation", "homelab",
         "Our API returned a lot of HTTP 500 errors on 2026-09-12. logs/access.log has one line per request "
         "(including the user and a request id), and logs/app.log has application log lines keyed by request id. "
         "Which single user caused the most 500 responses, exactly how many 500 responses did that user get, and "
         "what error does the application log for those requests? The logs are large, so use scripts or shell "
         "tools rather than reading them in full.",
         lambda: corr_data()[0], corr_check, lambda ctx: "u0271 had {count} 500s, all MemoryError".format(**corr_data()[1])),
    Task("sqlite_report", "shell",
         "data/shop.db is a SQLite database of customers, orders and refunds. Compute each customer's net revenue "
         "for Q2 2026 (orders placed from 2026-04-01 through 2026-06-30 inclusive, only orders with status `paid`, "
         "minus all refunds recorded against those orders). Write the top 5 customers to out/top_customers.json as "
         "a JSON array of objects `{\"customer\": <name>, \"net_revenue_cents\": <int>}`, highest net revenue "
         "first (break ties by name). Python's sqlite3 module is available; the sqlite3 command-line tool is not.",
         lambda: {"README.md": "# shop analytics\n"}, sql_check, sql_solve, setup=sql_setup),
    Task("perf_fix", "code",
         "pipeline/events.py is far too slow on real data (hundreds of thousands of events). Make both functions "
         "fast (they should handle ~100k inputs in well under a second each) without changing their behavior, "
         "including the details in the docstrings. Do not modify the existing tests; they must keep passing.",
         lambda: {"pipeline/__init__.py": "", "pipeline/events.py": PERF_PY, "tests/test_events.py": PERF_TESTS},
         perf_check, perf_solve),
    Task("compose_diagnosis", "homelab",
         "Read NOTES.md. Using only the files here (docker is not available), find every reason the affected "
         "Prometheus jobs can't be scraped and fix the configuration. Prometheus must scrape the other services "
         "over the compose network by service name. Don't change host port mappings or remove any jobs.",
         lambda: {"docker-compose.yml": COMPOSE_YML, "prometheus/prometheus.yml": PROM_YML, "NOTES.md": COMPOSE_NOTES},
         compose_check, compose_solve),
    Task("flaky_shared_state", "debug",
         "The tests in this repo pass when each test file is run on its own but fail when the whole suite runs. "
         "Find the root cause in the code and fix it. Do not modify the tests.",
         lambda: {"shop/__init__.py": "", "shop/cart.py": CART_PY, "tests/test_cart_a.py": CART_TEST_A,
                  "tests/test_cart_b.py": CART_TEST_B},
         cart_check, cart_solve),
    Task("cli_json_output", "code", CLI_SPEC, cli_files, cli_check, cli_solve),
    Task("trace_config_flow", "understand", TRACE_PROMPT, lambda: dict(TRACE_FILES), trace_check,
         lambda ctx: "living_room in production: 80 (90 clamped). bedroom with no profile set: 65."),
]

for _task in HARD_TASKS:
    _task.max_turns, _task.wall_limit = HARD_LIMITS["max_turns"], HARD_LIMITS["wall_limit"]

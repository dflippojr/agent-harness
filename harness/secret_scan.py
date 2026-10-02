"""Secret scanning of a session's added lines before Review push/merge (issue #263).

gitleaks (MIT) runs host-side as one pinned release (harness/gitleaks/pin.json) with the harness-owned rules file
next to the pin. It never sees a workspace config: it reads the added lines on stdin, from an empty temporary
directory, with `--config` explicit, `--ignore-gitleaks-allow`, no baseline, GITLEAKS_* variables removed, and
`--redact` so the value never reaches its output.

A finding is `{repo, file, line, rule, fingerprint, preview}`. `preview` keeps at most the first and last two
characters; `fingerprint` is a salted hash so a dismissal follows the same value to later heads. Each commit since
the base is scanned too (a push sends them all): a value only found in a commit's own diff, i.e. one a later commit
removed, adds a finding with that commit's short SHA in `commit` and in its fingerprint. The raw value
only exists inside `_findings` while a finding is built; the scanner's own output is dropped after masking.
Results are cached per (salt, head, diff hash, commit range, pin) and hold diff positions (for masking the Changes diff), never
values.

    python -m harness.secret_scan install [--config-dir DIR]   fetch + verify the pinned binary (installers)
    python -m harness.secret_scan status [--config-dir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from pathlib import Path

from .review_comments import HUNK, _path

PIN_DIR = Path(__file__).parent / "gitleaks"
PIN = json.loads((PIN_DIR / "pin.json").read_text(encoding="utf-8"))
VERSION = PIN["version"]
RULES = PIN_DIR / PIN["rules"]["file"]
SCANNER = f"gitleaks {VERSION}"
TIMEOUT = 120
CACHE_SIZE = 128
MASK = "…"
SHORT_SHA = 12
CANCELLED = "the gitleaks install was cancelled (daemon stopping)"


def rules_sha256(path: Path = RULES) -> str:
    """sha256 of the rules with LF endings, so a CRLF checkout still matches the pin."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def asset_key() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    osname = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    return f"{osname}_{arch}"


def mask(value: str) -> str:
    """At most the first and last two characters; short values show nothing."""
    return f"{value[:2]}{MASK}{value[-2:]}" if len(value) >= 10 else MASK


class ScannerUnavailable(Exception):
    pass


class Scanner:
    """The pinned gitleaks binary under `<data_dir>/tools`, plus the result cache."""

    def __init__(self, tools_dir: Path):
        self.dir = Path(tools_dir) / f"gitleaks-{VERSION}"
        self.binary = self.dir / ("gitleaks.exe" if sys.platform == "win32" else "gitleaks")
        self.error_file = Path(tools_dir) / "gitleaks-bootstrap-error.txt"
        self._checked: tuple | None = None
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.cancelled = threading.Event()  # daemon stop: a fetch still running afterwards writes nothing

    # ---------------------------------------------------------------- install / status
    def problem(self) -> str:
        """'' when the pinned binary and rules are usable, else what is wrong (no values, safe to show)."""
        if rules_sha256() != PIN["rules"]["sha256"]:
            return f"the secret-scan rules file {RULES.name} does not match its pin"
        try:
            st = self.binary.stat()
        except OSError:
            return f"gitleaks {VERSION} is not installed at {self.binary}"
        stamp = (st.st_size, st.st_mtime_ns)
        if self._checked and self._checked[0] == stamp:
            return self._checked[1]
        try:
            out = subprocess.run([str(self.binary), "version"], capture_output=True, text=True, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"gitleaks at {self.binary} does not run ({type(e).__name__})"
        found = out.removeprefix("v")
        problem = "" if found == VERSION else f"gitleaks at {self.binary} is version {found or '?'}, pinned {VERSION}"
        self._checked = (stamp, problem)
        return problem

    def install(self, fetch=None) -> str:
        """Fetch the pinned release, verify its SHA-256, and extract the binary. Returns '' or the error."""
        key = asset_key()
        asset = PIN["assets"].get(key)
        if asset is None:
            return self._record(f"no pinned gitleaks build for {key}")
        try:
            data = (fetch or _download)(PIN["url"].format(name=asset["name"]))
        except Exception as e:  # noqa: BLE001 - any fetch failure leaves push/merge blocked
            return self._record(f"could not download {asset['name']}: {type(e).__name__}: {e}")
        if self.cancelled.is_set():
            return CANCELLED
        got = hashlib.sha256(data).hexdigest()
        if got != asset["sha256"]:
            return self._record(f"checksum mismatch for {asset['name']}: got {got}, pinned {asset['sha256']}")
        try:
            binary = _extract(data, asset["name"], self.binary.name)
        except (OSError, KeyError, tarfile.TarError, zipfile.BadZipFile) as e:
            return self._record(f"could not extract {asset['name']}: {e}")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.binary.with_name(f"{self.binary.name}.{os.getpid()}.part")
        tmp.write_bytes(binary)
        tmp.chmod(0o755)
        if self.cancelled.is_set():
            tmp.unlink(missing_ok=True)
            return CANCELLED
        try:
            os.replace(tmp, self.binary)
        except OSError:  # another process installed it meanwhile and Windows locks a running .exe
            tmp.unlink(missing_ok=True)
        self._checked = None
        problem = self.problem()
        if not problem:
            self.error_file.unlink(missing_ok=True)
        return self._record(problem) if problem else ""

    def ensure(self, fetch=None) -> str:
        """Daemon start: install the pinned binary when it is missing or the wrong version."""
        problem = self.problem()
        if not problem or "rules file" in problem:
            return problem
        return self.install(fetch)

    def bootstrap_error(self) -> str:
        try:
            return self.error_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _record(self, message: str) -> str:
        if self.cancelled.is_set():
            return message
        try:
            self.error_file.parent.mkdir(parents=True, exist_ok=True)
            self.error_file.write_text(message + "\n", encoding="utf-8")
        except OSError:
            pass
        return message

    # ---------------------------------------------------------------- scanning
    def scan(self, repos: list[dict], salt: str) -> dict:
        """Scan the added lines of each repo's diff and of each of its commits: repos = [{path, head, diff,
        commits: [{sha, diff}]}].

        Returns {status: ok|unavailable|error, message, scanner, findings, cached, elapsed_ms}. Each finding
        carries a private `_spans` list [(diff line index, byte start, byte end)] for `redact`."""
        started = time.perf_counter()
        key = (salt, VERSION, PIN["rules"]["sha256"],
               tuple((r["path"], r.get("head", ""), hashlib.sha256(r["diff"].encode()).hexdigest(),
                      tuple(c["sha"] for c in r.get("commits", ()))) for r in repos))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
        if hit is not None:
            return {**hit, "cached": True, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
        result = {"status": "ok", "message": "", "scanner": SCANNER, "findings": []}
        if problem := self.problem():
            return {**result, "status": "unavailable", "message": problem, "cached": False, "elapsed_ms": 0}
        # Commits come after the repos, so a commit finding's spans never match a repo index in `redact`.
        entries = [*repos, *({"path": r["path"], "diff": c["diff"], "commit": c["sha"][:SHORT_SHA]}
                             for r in repos for c in r.get("commits", ()))]
        text, where = scan_input(entries)
        if len(where) > 1:
            try:
                result["findings"] = _findings(self._run(text), text, where, entries, salt)
            except ScannerUnavailable as e:
                return {**result, "status": "error", "message": str(e), "cached": False, "elapsed_ms": 0}
        with self._lock:
            self._cache[key] = result
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return {**result, "cached": False, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}

    def _run(self, text: str) -> list[dict]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GITLEAKS")}
        with tempfile.TemporaryDirectory(prefix="harness-gitleaks-") as empty:
            cmd = [str(self.binary), "stdin", "--config", str(RULES), "--gitleaks-ignore-path", empty,
                   "--ignore-gitleaks-allow", "--redact", "--no-banner", "--no-color", "--log-level", "error",
                   "--report-format", "json", "--report-path", "-", "--exit-code", "0"]
            try:
                p = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, cwd=empty, env=env,
                                   timeout=TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                raise ScannerUnavailable(f"gitleaks timed out after {TIMEOUT} s") from None
            except OSError as e:
                raise ScannerUnavailable(f"gitleaks did not start ({type(e).__name__})") from None
        if p.returncode != 0:
            # stderr is gitleaks' own log at error level; with --redact it carries no values, but keep it short.
            raise ScannerUnavailable(f"gitleaks exited {p.returncode}: "
                                     f"{p.stderr.decode('utf-8', 'replace').strip()[-300:]}")
        try:
            report = json.loads(p.stdout or b"[]")
        except ValueError:
            raise ScannerUnavailable("gitleaks wrote an unreadable report") from None
        if not isinstance(report, list):
            raise ScannerUnavailable("gitleaks wrote an unexpected report")
        return report


def scan_input(repos: list[dict]) -> tuple[str, list]:
    """The added lines of every diff as one text, and where each input line came from.

    `where[i]` is (repo index, file, new line number, diff line index) for input line i + 1, or None for a blank
    separator. A separator opens the input (so gitleaks' column quirk is the same on every line) and sits between
    runs of added lines that aren't adjacent, so a multi-line rule can't join unrelated lines."""
    lines: list[str] = [""]
    where: list = [None]
    for ri, repo in enumerate(repos):
        name, new, in_hunk = "", 0, False
        for di, raw in enumerate(repo["diff"].split("\n")):
            if raw.startswith("diff --git "):
                name, in_hunk = "", False
            elif not in_hunk and raw.startswith("+++ "):
                name = _path("+++ ", raw) or ""
            elif raw.startswith("@@"):
                m = HUNK.match(raw)
                in_hunk, new = bool(m), int(m.group(2)) if m else 0
            elif in_hunk and raw.startswith("+"):
                last = where[-1]
                if last is None or (last[0], last[1], last[2] + 1) != (ri, name, new):
                    if last is not None:
                        lines.append("")
                        where.append(None)
                lines.append(raw[1:].rstrip("\r"))
                where.append((ri, name, new, di))
                new += 1
            elif in_hunk and raw.startswith(" "):
                new += 1
    return "\n".join(lines) + "\n", where


def _spans(text_lines: list[bytes], where: list, start: int, end: int, col0: int, col1: int) -> list[tuple]:
    """Diff positions covered by input lines start..end (1-based), from byte col0 on the first to col1 on the last."""
    out = []
    for n in range(start, end + 1):
        w = where[n - 1] if 0 < n <= len(where) else None
        if w is None:
            continue
        b0 = col0 if n == start else 0
        b1 = col1 if n == end else len(text_lines[n - 1])
        out.append((w[0], w[3], b0, max(b0, b1)))
    return out


def _secret_in(matched: str, redacted_match: str) -> tuple[str, int, int]:
    """(value, prefix bytes, suffix bytes) inside the matched text, from where --redact put REDACTED.

    The whole match when unsure."""
    prefix, sep, suffix = redacted_match.partition("REDACTED")
    if (sep and "REDACTED" not in suffix and matched.startswith(prefix) and matched.endswith(suffix)
            and len(matched) > len(prefix) + len(suffix)):
        return matched[len(prefix):len(matched) - len(suffix)], len(prefix.encode()), len(suffix.encode())
    return matched, 0, 0


def _findings(report: list[dict], text: str, where: list, repos: list[dict], salt: str) -> list[dict]:
    text_lines = [ln.encode("utf-8") for ln in text.split("\n")]
    out = []
    for item in report:
        try:
            start, end = int(item["StartLine"]), int(item["EndLine"])
            # gitleaks columns are 1-based bytes, offset by one more after line 1 (the input opens with a blank).
            col0, col1 = int(item["StartColumn"]) - 2, int(item["EndColumn"]) - 1
            rule = str(item["RuleID"])
        except (KeyError, TypeError, ValueError):
            continue
        origin = where[start - 1] if 0 < start <= len(where) else None
        if origin is None or end < start:
            continue
        first = text_lines[start - 1]
        if not 0 <= col0 < len(first):
            col0 = 0
        end = min(end, len(where))
        matched = b"\n".join(text_lines[n - 1][col0 if n == start else 0:col1 if n == end else None]
                             for n in range(start, end + 1))
        value, head, tail = _secret_in(matched.decode("utf-8", "replace"), str(item.get("Match") or ""))
        if start == end:  # mask just the value, not a `KEY = "..."` around it
            col0, col1 = col0 + head, col1 - tail
        spans = _spans(text_lines, where, start, end, col0, max(col1, 0))
        ri, file, line = origin[0], origin[1], origin[2]
        repo, commit = repos[ri]["path"], repos[ri].get("commit", "")
        fingerprint = hashlib.sha256("\0".join((salt, repo, file, rule, value) + ((commit,) if commit else ()))
                                     .encode()).hexdigest()
        finding = {"repo": repo, "file": file, "line": line, "rule": rule, "fingerprint": fingerprint[:20],
                   "preview": mask(value), "_spans": spans,
                   "_value": hashlib.sha256("\0".join((salt, repo, rule, value)).encode()).hexdigest()}
        out.append({**finding, "commit": commit} if commit else finding)
    # A value still in the working diff is that finding; one only in commits is reported once, at its first commit.
    seen = {f["_value"] for f in out if "commit" not in f}
    kept = []
    for f in out:  # repos, then commits oldest first
        if "commit" not in f or f["_value"] not in seen:
            seen.add(f["_value"])
            kept.append({k: v for k, v in f.items() if k != "_value"})
    kept.sort(key=lambda f: (f["repo"], "commit" in f, f["file"], f["line"], f["rule"]))
    return kept


def public(result: dict) -> dict:
    """A scan result without diff positions, for API responses."""
    return {**result, "findings": [{k: v for k, v in f.items() if k != "_spans"} for f in result["findings"]]}


def redact(diff: str, repo_index: int, findings: list[dict]) -> str:
    """Mask every flagged byte range in one repo's diff (positions from `scan`, which was given this diff)."""
    targets: dict[int, list[tuple[int, int, str]]] = {}
    for f in findings:
        for ri, di, b0, b1 in f.get("_spans", ()):
            if ri == repo_index:
                targets.setdefault(di, []).append((b0, b1, f["preview"]))
    if not targets:
        return diff
    lines = diff.split("\n")
    for di, ranges in targets.items():
        if di >= len(lines):
            continue
        body = lines[di][1:].encode("utf-8")
        clamped = []
        for b0, b1, preview in ranges:
            b0, b1 = max(0, min(b0, len(body))), max(0, min(b1, len(body)))
            # unknown position: hide the whole line rather than risk showing the value
            clamped.append((b0, b1, preview) if b1 > b0 else (0, len(body), preview))
        merged: list[list] = []
        for b0, b1, preview in sorted(clamped):
            if merged and b0 < merged[-1][1]:  # two rules matched overlapping text
                merged[-1][1] = max(merged[-1][1], b1)
            else:
                merged.append([b0, b1, preview])
        for b0, b1, preview in reversed(merged):
            body = body[:b0] + f"[secret {preview}]".encode() + body[b1:]
        lines[di] = "+" + body.decode("utf-8", "replace")
    return "\n".join(lines)


def _download(url: str) -> bytes:
    import httpx
    r = httpx.get(url, follow_redirects=True, timeout=120)
    r.raise_for_status()
    return r.content


def _extract(data: bytes, archive: str, member: str) -> bytes:
    if archive.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return z.read(member)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        f = t.extractfile(member)
        if f is None:
            raise KeyError(member)
        return f.read()


def tools_dir(cfg) -> Path:
    return Path(cfg.data_dir) / "tools"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install or check the pinned secret scanner (gitleaks)")
    ap.add_argument("command", choices=("install", "status"))
    ap.add_argument("--config-dir")
    args = ap.parse_args(argv)
    from . import config as config_mod
    scanner = Scanner(tools_dir(config_mod.load(args.config_dir)))
    problem = scanner.ensure() if args.command == "install" else scanner.problem()
    print(problem or f"{SCANNER} ready at {scanner.binary}")
    return 1 if problem else 0


if __name__ == "__main__":
    sys.exit(main())

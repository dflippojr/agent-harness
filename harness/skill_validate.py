"""Deterministic v1 skill-package validator. Stdlib only so it can run inside an isolated sandbox.

The sandbox copies this file in and runs ``python /run/validate.py /proposal``. It reads the mounted
proposal as data and never executes proposal text, scripts, or references.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

VALIDATOR_VERSION = "1"
MAX_SKILL_MD_BYTES = 32 * 1024
MAX_REFERENCE_FILES = 16
MAX_REFERENCE_BYTES = 16 * 1024
MAX_TOTAL_BYTES = 128 * 1024
MAX_EXAMPLES = 5
MIN_EXAMPLES = 2
MAX_EXAMPLE_CHARS = 4000
MAX_TITLE = 80
MAX_PURPOSE = 500
MAX_ACTIVATION = 400
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
REF_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}\.md$")
SKILL_MD = "SKILL.md"
MANIFEST_JSON = "manifest.json"
EXAMPLES_JSON = "examples.json"
ALLOWED_ROOT_FILES = frozenset({SKILL_MD, MANIFEST_JSON, EXAMPLES_JSON})
FORBIDDEN_SUFFIXES = frozenset({
    ".py", ".pyw", ".pyc", ".pyo", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".exe", ".dll",
    ".so", ".dylib", ".bin", ".com", ".msi", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".wasm",
    ".html", ".htm", ".xhtml", ".svg", ".xml", ".css", ".ipynb", ".jar", ".class", ".whl", ".egg",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".ico", ".bmp", ".pdf", ".docx", ".xlsx", ".pptx", ".lock", ".toml", ".cfg", ".ini",
})
FORBIDDEN_NAMES = frozenset({
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "requirements.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "pipfile.lock", "gemfile", "cargo.toml",
    "makefile", "dockerfile", "compose.yaml", "compose.yml", "docker-compose.yml",
})
REMOTE_INCLUDE_RES = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"!\[.*?\]\(\s*(?:https?|file|data):",
    r"<(?:script|iframe|object|embed|link)\b",
    r"\b(?:include|import|require)\s*::",
    r"\{%\s*include\b",
    r"\bfrom\s+['\"]https?://",
    r"\]\(\s*javascript:",
    r"src\s*=\s*['\"]https?://",
))
SECRET_RES = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"(?a)\b(?:ghp|gho|ghu|ghs|ghr)_\w{20,}\b")),
    ("github-pat", re.compile(r"(?a)\bgithub_pat_\w{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{16,}\b")),
    ("generic-secret", re.compile(
        r"(?i)\b((?:api|secret)[_ -]?key|(?:access|auth)[_ -]?token|password|passwd)\b\s*[:=]\s*\S{8,}")),
]
POLICY_RES = [
    ("approval-bypass", re.compile(
        r"(?i)\b(always.?allow|auto-?approve|skip (?:the )?(?:user )?approval|don't ask(?: the user)?(?: for approval)?"
        r"|never ask(?: the user)?|set (?:the )?approval(?: policy)? to allow|bypass (?:the )?(?:user )?approval)\b")),
    ("sandbox-bypass", re.compile(
        r"(?i)\b(disable (?:the )?sandbox|bypass (?:the )?sandbox|no-?new-?privileges|mount docker\.sock|"
        r"privileged container|network:\s*true without asking|always (?:enable|set) network)\b")),
    ("policy-bypass", re.compile(
        r"(?i)\b(ignore (?:all |any )?(?:previous |earlier |system |daemon |owner )?(?:instructions|policy|rules)|"
        r"override (?:the )?(?:system|daemon|owner) (?:prompt|instructions|policy)|"
        r"you are now unconstrained|jailbreak|developer mode)\b")),
    ("credential-access", re.compile(
        r"(?i)\b(read (?:the )?(?:api key|secret|token|credential|password)|exfiltrat|"
        r"send (?:the )?(?:secret|credential|api key)|provider.?auth|harness\.local\.yaml|"
        r"secrets?/(?:claude|codex|cursor))\b")),
    ("self-modification", re.compile(
        r"(?i)\b(install (?:this|the) skill|enable (?:this|the) skill|write (?:to )?the skill store|"
        r"modify (?:your|the agent's) (?:system )?prompt|self-?modif|replicate (?:this|yourself)|"
        r"copy this skill into)\b")),
    ("hidden-persistence", re.compile(
        r"(?i)\b(hidden (?:file|instruction|persist)|do not (?:tell|show) the (?:user|owner)|"
        r"conceal (?:this|these) instructions|ignore this comment)\b")),
    ("unbounded-external", re.compile(
        r"(?i)\b(unbounded (?:network|web|external)|call any (?:url|host)|disable egress|"
        r"exfiltrate to)\b")),
]
HTML_RE = re.compile(r"(?i)</?(?:html|script|iframe|object|embed|form)\b")
FRONTMATTER_NAME_RE = re.compile(r"(?im)^(?:name|slug)\s*:\s*(\S+)")


class Finding(dict):
    @staticmethod
    def make(code: str, message: str, path: str = "") -> dict:
        item = {"code": code, "message": message}
        if path:
            item["path"] = path
        return item


def normalize_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()).strip("-")


def canonical_hash(bundle: dict) -> str:
    """SHA-256 of the exact owner-visible bytes: files, examples, and identifying fields."""
    payload = {
        "activation_suggestion": bundle.get("activation_suggestion") or "",
        "examples": bundle.get("examples") or [],
        "files": {path: bundle["files"][path] for path in sorted(bundle.get("files") or {})},
        "purpose": bundle.get("purpose") or "",
        "slug": bundle.get("slug") or "",
        "title": bundle.get("title") or "",
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink() or os.path.islink(path):
        return True
    try:
        st = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if os.name == "nt":
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            return True
        try:
            import ctypes
            get_attrs = ctypes.windll.kernel32.GetFileAttributesW
            raw = get_attrs(str(path))
            return raw != -1 and bool(raw & FILE_ATTRIBUTE_REPARSE_POINT)
        except Exception:
            return False
    return False


def _utf8_text(raw: bytes, path: str, findings: list[dict]) -> str | None:
    if b"\x00" in raw:
        findings.append(Finding.make("binary", "file contains NUL bytes (binary content is forbidden)", path))
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        findings.append(Finding.make("not-utf8", "file is not valid UTF-8 text", path))
        return None
    if "\ufffd" in text and raw.count(b"\xef\xbf\xbd") == 0:
        findings.append(Finding.make("not-utf8", "file is not valid UTF-8 text", path))
        return None
    return text


def _scan_text(text: str, path: str, findings: list[dict]) -> None:
    if any(rx.search(text) for rx in REMOTE_INCLUDE_RES) or HTML_RE.search(text):
        findings.append(Finding.make("remote-include", "HTML, remote includes, or unsafe links are forbidden", path))
    for code, rx in SECRET_RES:
        if rx.search(text):
            findings.append(Finding.make("secret", f"secret-like {code} pattern is forbidden in instruction skills", path))
            break
    for code, rx in POLICY_RES:
        if rx.search(text):
            findings.append(Finding.make(code, "instructions that weaken policy, sandbox, credentials, or self-modify are forbidden", path))


def _check_path(rel: str, root: Path, findings: list[dict]) -> Path | None:
    if not rel or rel.startswith("/") or rel.startswith("\\") or ":" in rel.split("/", 1)[0]:
        findings.append(Finding.make("traversal", "absolute paths are forbidden", rel))
        return None
    if "\\" in rel:
        findings.append(Finding.make("traversal", "use forward slashes in skill paths", rel))
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        findings.append(Finding.make("traversal", "'.' / '..' / empty path segments are forbidden", rel))
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        findings.append(Finding.make("traversal", "path escapes the proposal directory", rel))
        return None
    return root.joinpath(*parts)


def _check_file(path, content, names_lower: dict[str, str], findings: list[dict]) -> tuple[int, int]:
    """Validate one bundle file; returns (bytes to count toward the total, 1 if it is a reference file)."""
    if not isinstance(path, str) or not isinstance(content, str):
        findings.append(Finding.make("files", "each file path and body must be a string"))
        return 0, 0
    lowered = path.lower()
    if lowered in names_lower and names_lower[lowered] != path:
        findings.append(Finding.make("case-collision", f"file name collides with {names_lower[lowered]}", path))
    names_lower[lowered] = path
    parts = path.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts) or path.startswith(("/", "\\")) or "\\" in path:
        findings.append(Finding.make("traversal", "paths must stay inside the skill package", path))
        return 0, 0
    raw = content.encode("utf-8")
    if path != SKILL_MD:
        _utf8_text(raw, path, findings)
    suffix = Path(path.lower()).suffix
    name = Path(path.lower()).name
    if suffix in FORBIDDEN_SUFFIXES or name in FORBIDDEN_NAMES:
        findings.append(Finding.make("forbidden-type", f"{name} is not allowed in a v1 instruction skill", path))
    if path in (SKILL_MD, MANIFEST_JSON):
        return len(raw), 0
    if not path.startswith("references/") or path.count("/") != 1:
        findings.append(Finding.make("path", "only SKILL.md, manifest.json, and references/*.md are allowed", path))
        return len(raw), 0
    ref_name = path.split("/", 1)[1]
    if not REF_NAME_RE.fullmatch(ref_name):
        findings.append(Finding.make("path", "reference files must be references/<lowercase-name>.md", path))
    if len(raw) > MAX_REFERENCE_BYTES:
        findings.append(Finding.make("oversize", f"reference exceeds {MAX_REFERENCE_BYTES} bytes", path))
    _scan_text(content, path, findings)
    return len(raw), 1


def _check_examples(examples, findings: list[dict]) -> None:
    if not isinstance(examples, list) or not (MIN_EXAMPLES <= len(examples) <= MAX_EXAMPLES):
        findings.append(Finding.make("examples", f"provide {MIN_EXAMPLES}–{MAX_EXAMPLES} example prompts with expected behavior"))
        return
    for i, item in enumerate(examples):
        if not isinstance(item, dict):
            findings.append(Finding.make("examples", f"example {i + 1} must be an object"))
            continue
        prompt = str(item.get("prompt") or "").strip()
        expected = str(item.get("expected") or item.get("expected_behavior") or "").strip()
        if not prompt or not expected:
            findings.append(Finding.make("examples", f"example {i + 1} needs prompt and expected behavior"))
        if len(prompt) > MAX_EXAMPLE_CHARS or len(expected) > MAX_EXAMPLE_CHARS:
            findings.append(Finding.make("examples", f"example {i + 1} is too long"))
        _scan_text(prompt + "\n" + expected, f"examples[{i}]", findings)


def _check_header(bundle: dict, findings: list[dict]) -> tuple[str, str, str, str]:
    slug = normalize_slug(str(bundle.get("slug") or ""))
    if not SLUG_RE.fullmatch(slug):
        findings.append(Finding.make("slug", "slug must be 2–40 lowercase letters, numbers, or dashes"))
    title = str(bundle.get("title") or "").strip()
    if not title or len(title) > MAX_TITLE:
        findings.append(Finding.make("title", f"title is required and at most {MAX_TITLE} characters"))
    purpose = str(bundle.get("purpose") or "").strip()
    if not purpose or len(purpose) > MAX_PURPOSE:
        findings.append(Finding.make("purpose", f"purpose is required and at most {MAX_PURPOSE} characters"))
    activation = str(bundle.get("activation_suggestion") or "").strip()
    if len(activation) > MAX_ACTIVATION:
        findings.append(Finding.make("activation", f"activation suggestion is at most {MAX_ACTIVATION} characters"))
    return slug, title, purpose, activation


def _check_skill_md(files: dict, findings: list[dict]) -> str:
    skill_md = files.get(SKILL_MD)
    if not isinstance(skill_md, str) or not skill_md.strip():
        findings.append(Finding.make("skill-md", "SKILL.md is required", SKILL_MD))
        skill_md = skill_md if isinstance(skill_md, str) else ""
    if len(skill_md.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        findings.append(Finding.make("oversize", f"SKILL.md exceeds {MAX_SKILL_MD_BYTES} bytes", SKILL_MD))
    return skill_md


def _check_skill_md_text(skill_md: str, slug: str, findings: list[dict]) -> None:
    _scan_text(skill_md, SKILL_MD, findings)
    fm_name = FRONTMATTER_NAME_RE.search(skill_md)
    if fm_name and normalize_slug(fm_name.group(1)) not in ("", slug):
        findings.append(Finding.make("slug-mismatch", "SKILL.md name/slug does not match the proposal slug", SKILL_MD))


def validate_bundle(bundle: dict) -> dict:
    """Validate an in-memory instruction-only skill. Never executes file contents."""
    findings: list[dict] = []
    slug, title, purpose, activation = _check_header(bundle, findings)

    files = bundle.get("files") or {}
    if not isinstance(files, dict):
        findings.append(Finding.make("files", "files must be a mapping of path → UTF-8 text"))
        files = {}
    skill_md = _check_skill_md(files, findings)

    names_lower: dict[str, str] = {}
    total = 0
    ref_count = 0
    for path, content in files.items():
        added, is_ref = _check_file(path, content, names_lower, findings)
        total += added
        ref_count += is_ref
    if ref_count > MAX_REFERENCE_FILES:
        findings.append(Finding.make("count", f"at most {MAX_REFERENCE_FILES} reference files are allowed"))
    if total > MAX_TOTAL_BYTES:
        findings.append(Finding.make("oversize", f"total skill bytes exceed {MAX_TOTAL_BYTES}"))
    if skill_md:
        _check_skill_md_text(skill_md, slug, findings)

    examples = bundle.get("examples") or []
    _check_examples(examples, findings)

    _scan_text(f"{title}\n{purpose}\n{activation}", "manifest", findings)

    normalized = {
        "slug": slug,
        "title": title,
        "purpose": purpose,
        "activation_suggestion": activation,
        "files": {k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, str)},
        "examples": examples if isinstance(examples, list) else [],
    }
    content_hash = canonical_hash(normalized) if slug and title and SKILL_MD in normalized["files"] else ""
    codes = {f["code"] for f in findings}
    return {
        "ok": not findings,
        "validator_version": VALIDATOR_VERSION,
        "slug": slug,
        "content_hash": content_hash,
        "findings": findings,
        "codes": sorted(codes),
    }


def _load_sidecar_json(path: Path, findings: list[dict]) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        findings.append(Finding.make("manifest", f"{path.name} is not valid UTF-8 JSON: {exc}", path.name))
        return {}
    return data if isinstance(data, dict) else {}


def validate_dir(root: Path) -> dict:
    """Read a proposal directory as data. Never follows links or executes files."""
    root = Path(root)
    findings: list[dict] = []
    if not root.is_dir():
        return {"ok": False, "validator_version": VALIDATOR_VERSION, "slug": "", "content_hash": "",
                "findings": [Finding.make("missing", "proposal directory is missing")], "codes": ["missing"]}
    if _is_reparse_point(root):
        return {"ok": False, "validator_version": VALIDATOR_VERSION, "slug": "", "content_hash": "",
                "findings": [Finding.make("symlink", "proposal root may not be a symlink or junction")],
                "codes": ["symlink"]}

    files: dict[str, str] = {}
    seen_lower: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if _is_reparse_point(current):
            findings.append(Finding.make("symlink", "symlinks and junctions are forbidden",
                                         str(current.relative_to(root)).replace("\\", "/")))
            dirnames[:] = []
            continue
        dirnames[:] = sorted(dirnames)
        rel_dir = current.relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        for name in sorted(filenames):
            path = current / name
            rel = name if not rel_dir else f"{rel_dir}/{name}"
            if _is_reparse_point(path):
                findings.append(Finding.make("symlink", "symlinks and junctions are forbidden", rel))
                continue
            lowered = rel.lower()
            if lowered in seen_lower and seen_lower[lowered] != rel:
                findings.append(Finding.make("case-collision", f"collides with {seen_lower[lowered]}", rel))
            seen_lower[lowered] = rel
            if not path.is_file() or path.is_symlink():
                findings.append(Finding.make("forbidden-type", "only regular files are allowed", rel))
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                findings.append(Finding.make("read", f"could not read file: {exc}", rel))
                continue
            text = _utf8_text(raw, rel, findings)
            if text is None:
                continue
            if rel in ALLOWED_ROOT_FILES or rel.startswith("references/"):
                files[rel] = text
            else:
                findings.append(Finding.make("forbidden-type", "only SKILL.md, manifest.json, examples.json, and references/*.md are allowed", rel))

    meta = _load_sidecar_json(root / MANIFEST_JSON, findings)
    examples_file = _load_sidecar_json(root / EXAMPLES_JSON, findings)
    examples = meta.get("examples") if isinstance(meta.get("examples"), list) else examples_file.get("examples")
    bundle = {
        "slug": meta.get("slug") or "",
        "title": meta.get("title") or "",
        "purpose": meta.get("purpose") or "",
        "activation_suggestion": meta.get("activation_suggestion") or "",
        "files": {k: v for k, v in files.items() if k not in (MANIFEST_JSON, EXAMPLES_JSON)},
        "examples": examples or [],
    }
    result = validate_bundle(bundle)
    result["findings"] = findings + result["findings"]
    result["ok"] = not result["findings"]
    result["codes"] = sorted({f["code"] for f in result["findings"]})
    return result


SANDBOX_WORK = "/sandbox-work"


def _container_user() -> str:
    """Match the host uid so owner-only staging modes remain readable inside the container."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if callable(getuid) and callable(getgid):
        return f"{getuid()}:{getgid()}"
    return "65534:65534"


def sandbox_command(image: str, proposal_dir: Path, validator_path: Path) -> list[str]:
    """Fresh no-network, read-only container. Proposal and validator only; nothing executable from the proposal."""
    proposal = proposal_dir.resolve()
    validator = validator_path.resolve()
    work = SANDBOX_WORK
    return [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", f"{work}:rw,noexec,nosuid,size=16m",
        "--security-opt", "no-new-privileges",
        "--cap-drop", "ALL",
        "--pids-limit", "64",
        "--memory", "256m",
        "--cpus", "1",
        "--user", _container_user(),
        "--mount", f"type=bind,source={proposal},target=/proposal,readonly",
        "--mount", f"type=bind,source={validator},target=/run/validate.py,readonly",
        "--workdir", work,
        "-e", f"TMPDIR={work}",
        "-e", f"PYTHONPYCACHEPREFIX={work}",
        image,
        "python", "/run/validate.py", "/proposal",
    ]


def sandbox_command_is_isolated(argv: list[str]) -> list[str]:
    """Return reasons the docker argv would violate the v1 sandbox contract."""
    reasons = []
    joined = " ".join(argv)
    if "--network" not in argv or "none" not in argv:
        reasons.append("network is not disabled")
    if "--read-only" not in argv:
        reasons.append("root filesystem is not read-only")
    if "no-new-privileges" not in joined:
        reasons.append("new privileges are not blocked")
    if "--cap-drop" not in argv or "ALL" not in argv:
        reasons.append("capabilities are not dropped")
    forbidden_needles = (
        "docker.sock", "/var/run/docker", "/workspace", "provider-auth", "harness-auth",
        "/secrets", "memory-library", "com.docker.desktop", "\\\\.\\pipe\\docker",
    )
    for needle in forbidden_needles:
        if needle.lower() in joined.lower():
            reasons.append(f"forbidden mount or path {needle}")
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount" and i + 1 < len(argv)]
    if len(mounts) != 2:
        reasons.append("sandbox must mount only the proposal and the validator")
    else:
        targets = " ".join(mounts)
        if "target=/proposal" not in targets or "target=/run/validate.py" not in targets:
            reasons.append("sandbox mounts must be /proposal and /run/validate.py")
        if any("readonly" not in m.replace(" ", "").lower() and "readonly" not in m for m in mounts):
            if not all("readonly" in m for m in mounts):
                reasons.append("proposal and validator mounts must be read-only")
    if any(a in argv for a in ("--privileged", "--pid=host", "--network=host")):
        reasons.append("host namespaces are forbidden")
    return reasons


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0] if args else "/proposal")
    result = validate_dir(root)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

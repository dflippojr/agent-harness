"""The Plex transcode allowance is count-only (#828, owner decision (a)): no guard code reads a transcode's source
resolution or HDR flag. A heavy transcode inside the allowance is left to the min_free_vram_mib backstop. Its own file:
the scan lists a package directory, and tests/test_ci_skip_list.py treats every path a listing file names as read."""
import re
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "harness_modules" / "local_model"
SOURCE_FIELDS = re.compile(r"resolution|hdr|tone.?map|\b4k\b|colorTrc|dovi", re.IGNORECASE)


def test_no_guard_code_reads_a_source_resolution_or_hdr_flag():
    files = sorted(GUARD.glob("*.py"))
    assert any(p.name == "service.py" for p in files)  # the scan looks where the guard lives
    hits = [f"{p.name}:{n}: {line.strip()}" for p in files
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if SOURCE_FIELDS.search(line)]
    assert hits == []

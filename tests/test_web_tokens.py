"""Design tokens and touch targets in the web app's stylesheet (#515).

Colours live in the token blocks at the top of style.css (:root, the dark media block and html[data-theme=…]); every
other rule paints with var(--…). The main text and accent pairs meet WCAG AA in the light and dark themes, and
interactive controls are at least 44 px tall.
"""
import functools
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "harness" / "web"
CSS = (WEB / "style.css").read_text(encoding="utf-8")

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(")
TOKEN_SELECTOR = re.compile(r'^(?::root(?::not\(\[data-theme\]\))?|html\[data-theme="[a-z]+"\])$')
TAP = 44


def _rules(css: str):
    """(selectors, body, at-rule prelude or "") for every style rule, comments removed, one level of @media."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, i = [], 0
    while True:
        open_ = css.find("{", i)
        if open_ < 0:
            return out
        head = css[i:open_].strip()
        if head.startswith("@"):
            depth, j = 1, open_ + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(css[j], 0)
                j += 1
            inner = css[open_ + 1:j - 1]
            if "{" in inner:
                out += [(sel, body, head) for sel, body, _ in _rules(inner)]
            i = j
            continue
        close = css.index("}", open_)
        out.append(([s.strip() for s in head.split(",")], css[open_ + 1:close], ""))
        i = close + 1


def _decls(body: str) -> dict:
    # Split on semicolons outside url("…") data URIs and parentheses.
    decls, depth, start, quote = {}, 0, 0, None
    for k, ch in enumerate(body + ";"):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            name, _, value = body[start:k].partition(":")
            if name.strip():
                decls[name.strip()] = value.strip()
            start = k + 1
    return decls


def _hard_coded(css: str):
    hits = []
    for selectors, body, _ in _rules(css):
        if all(TOKEN_SELECTOR.match(s) for s in selectors):
            continue
        for name, value in _decls(body).items():
            # data: URIs are icon masks; only their alpha is used and they carry no '#'.
            if COLOUR.search(re.sub(r'url\("data:[^"]*"\)', "", value)):
                hits.append(f"{', '.join(selectors)} {{ {name}: {value} }}")
    return hits


def test_no_hard_coded_colours_outside_the_token_blocks():
    hits = _hard_coded(CSS)
    assert not hits, "use a colour token from the top of style.css:\n" + "\n".join(hits)


def test_hard_coded_colour_detector():
    assert _hard_coded(".a { color: #fff; }")
    assert _hard_coded(".a { box-shadow: 0 1px 2px rgba(0,0,0,.2); }")
    assert _hard_coded("@media (min-width: 1px) { .a { background: hsl(0 0% 0%); } }")
    assert _hard_coded(".a { background: var(--x, #123456); }"), "a fallback colour is still hard-coded"
    assert not _hard_coded(":root { --x: #fff; } html[data-theme=\"dark\"] { --x: #000; }")
    assert not _hard_coded("#fab-host, #bad { color: var(--text); }"), "ids in selectors are not colours"
    assert not _hard_coded("/* #506 */ .a { -webkit-mask: url(\"data:image/svg+xml,%3Csvg stroke='black'%3E\"); }")


def _theme_blocks():
    blocks = {}
    for selectors, body, at in _rules(CSS):
        for sel in selectors:
            if TOKEN_SELECTOR.match(sel):
                key = (sel, "dark" if "prefers-color-scheme: dark" in at else "")
                blocks.setdefault(key, {}).update(
                    {k: v for k, v in _decls(body).items() if k.startswith("--")})
    return blocks


@functools.cache
def _themes():
    blocks = _theme_blocks()
    root = blocks[(":root", "")]
    themes = {"system light": dict(root),
              "system dark": {**root, **blocks[(":root:not([data-theme])", "dark")]}}
    for (sel, at), decls in blocks.items():
        named = re.match(r'html\[data-theme="([a-z]+)"\]', sel)
        if named and not at:
            themes[named.group(1)] = {**root, **decls}
    return themes


def _resolve(theme: dict, value: str) -> str:
    seen = 0
    while (m := re.fullmatch(r"var\((--[\w-]+)\)", value.strip())) and seen < 10:
        value, seen = theme[m.group(1)], seen + 1
    return value.strip()


def _rgb(value: str):
    hexa = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    assert hexa, f"contrast pairs must be opaque #rrggbb, got {value}"
    return [int(hexa.group(1)[k:k + 2], 16) / 255 for k in (0, 2, 4)]


def _luminance(value: str) -> float:
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in _rgb(value)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_formula():
    assert contrast("#000000", "#ffffff") == pytest.approx(21)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


# Foreground on background, all body-size text, so AA is 4.5:1.
PAIRS = [
    ("--text", "--bg"), ("--text", "--panel"), ("--text", "--panel-2"),
    ("--muted", "--bg"), ("--muted", "--panel"),
    ("--accent", "--bg"), ("--accent", "--panel"), ("--accent", "--info-bg"),
    ("--on-primary", "--primary"), ("--on-solid", "--ok-solid"),
    ("--ok", "--ok-bg"), ("--warn", "--warn-bg"), ("--bad", "--bad-bg"),
]


@pytest.mark.parametrize("theme", ["system light", "system dark", "light", "dark"])
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_main_pairs_meet_wcag_aa(theme, fg, bg):
    t = _themes()[theme]
    ratio = contrast(_resolve(t, t[fg]), _resolve(t, t[bg]))
    assert ratio >= 4.5, f"{theme}: {fg} on {bg} is {ratio:.2f}:1"


def test_dark_accent_is_a_colour_not_grey():
    for theme in ("system dark", "dark"):
        t = _themes()[theme]
        r, g, b = _rgb(_resolve(t, t["--accent"]))
        assert max(r, g, b) - min(r, g, b) > 0.2, f"{theme} accent reads as grey"


def test_explicit_themes_match_the_system_ones():
    themes = _themes()
    for name in ("light", "dark"):
        explicit, system = themes[name], themes[f"system {name}"]
        differ = {k for k in system if k.startswith("--") and _resolve(explicit, explicit[k]) != _resolve(system, system[k])}
        assert not differ, f"html[data-theme={name}] differs from the system {name} tokens: {sorted(differ)}"


def test_profile_swatches_follow_the_tokens():
    source = (WEB / "pages" / "profile.mjs").read_text(encoding="utf-8")
    themes = _themes()
    swatches = dict(re.findall(r'^\s*(\w+): \{ label: "[^"]+", swatch: \[("[^\]]+")\] \}', source, re.M))
    assert {"light", "dark", "midnight", "forest", "paper"} <= set(swatches)
    for name in ("light", "dark", "midnight", "forest", "paper"):
        colours = re.findall(r'"(#[0-9a-fA-F]{6})"', swatches[name])
        t = themes[name]
        want = [_resolve(t, t[k]).lower() for k in ("--bg", "--panel", "--accent")]
        assert [c.lower() for c in colours] == want, f"{name} swatch"
    assert swatches["auto"] == swatches["light"]


def test_theme_color_meta_follows_the_backgrounds():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    themes = _themes()
    for scheme in ("light", "dark"):
        meta = re.search(rf'<meta name="theme-color" content="(#[0-9a-fA-F]{{6}})" media="\(prefers-color-scheme: {scheme}\)">', html)
        assert meta and meta.group(1).lower() == themes[f"system {scheme}"]["--bg"].lower(), scheme


# --- touch targets ------------------------------------------------------------------------------------------------

INTERACTIVE = re.compile(r"(?:^|[\s>+~(,])(?:button|summary|select|input|textarea|a)\b(?![-\w])|\.(?:btn|tab|switch|icon|jump)(?![-\w])"
                         r"|-btn\b|-choice\b|-option\b|-item\b|-toggle\b|-retry\b|-action\b")
# Rules that size a part of a control (a track, a mask) or a non-interactive box that happens to match the pattern.
NOT_A_TARGET = {".switch::after", ".tabs.session-tabs", ".app-icon-choice .preview", ".app-icon-choice .preview img",
                ".theme-choice .swatch"}


def _px(value: str):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)px", value.strip())
    return float(m.group(1)) if m else None


def _small_targets(css: str):
    hits = []
    for selectors, body, at in _rules(css):
        if "pointer: fine" in at:
            continue
        decls = _decls(body)
        for sel in selectors:
            if sel in NOT_A_TARGET or "::" in sel or not INTERACTIVE.search(sel):
                continue
            for prop in ("height", "min-height"):
                size = _px(decls.get(prop, ""))
                if size is not None and size < TAP:
                    hits.append(f"{sel} {{ {prop}: {decls[prop]} }}")
    return hits


def test_interactive_controls_are_at_least_44px():
    hits = _small_targets(CSS)
    assert not hits, "touch targets must be at least 44 px (var(--tap)):\n" + "\n".join(hits)


def test_small_target_detector():
    assert _small_targets(".btn.small { min-height: 34px; }")
    assert _small_targets(".tabs button { height: 37px; }")
    assert _small_targets(".chat-pickers select { min-height: 40px; }")
    assert not _small_targets(".btn.small { min-height: var(--tap); }")
    assert not _small_targets(".tab-icon { width: 24px; height: 24px; }")
    assert not _small_targets(".progress { height: 6px; }")


def _rule(selector: str) -> dict:
    for selectors, body, at in _rules(CSS):
        if selector in selectors and not at:
            return _decls(body)
    raise AssertionError(f"{selector} missing from style.css")


def test_tap_token_and_the_audited_controls():
    assert _rule(":root")["--tap"] == f"{TAP}px"
    for selector in (".btn", ".btn.small", ".tabs button", ".emoji-choice", "summary", ".file > summary",
                     ".chat-composer .chat-pickers select", ".resolution-option"):
        decls = _rule(selector)
        assert decls.get("min-height") in ("var(--tap)", f"{TAP}px"), selector
    jump = _rule(".jump")
    assert jump["min-width"] == jump["min-height"] == "var(--tap)"
    switch = _rule(".switch")
    assert switch["height"] == "var(--tap)", "the switch's hit area is a full touch target"
    assert "content-box" in switch["background"], "only the 31 px track is painted"
    # The content box's corner radius is the outer radius minus the padding on each axis: 15.5 px wide, 22 - 6.5 px tall.
    assert switch["border-radius"] == "15.5px / calc(var(--tap) / 2)", "the painted track stays a capsule"


def test_fills_use_the_primary_token():
    for selector in (".btn.primary", ".btn.new-task", ".msg.user"):
        decls = _rule(selector)
        assert decls["background"] == "var(--primary)", selector
        assert decls["color"] == "var(--on-primary)", selector

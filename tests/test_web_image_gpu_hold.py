"""Images: GPU hold and installed models (#186), desktop gallery and details (#568)."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_image_model_filter_and_queue_generation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    root = Path(__file__).resolve().parents[1]
    script = Path(__file__).resolve().parent / "web_image_gpu_hold.mjs"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
    app = (root / "harness/web/pages/images.mjs").read_text(encoding="utf-8")
    css = (root / "harness/web/style.css").read_text(encoding="utf-8")
    assert "function installedImageModeEntries" in app
    assert "Queue Generation" in app
    assert "classList.toggle(\"queued\"" in app
    assert ".btn.queued" in css
    assert "var(--queued)" in css
    assert ".image-grid" in css
    assert "margin-top: 20px" in css
    assert "@media (max-width: 640px)" in css
    assert "margin-top: 24px" in css
    assert "ops/images-models.ps1" in app


def test_image_columns_keep_gallery_jobs_and_detail_actions():
    """Exercise the new containers, busy polling, and route teardown with real DOM helpers."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    root = Path(__file__).resolve().parents[1]
    script = r'''
import assert from "node:assert/strict";
import { mountImages } from "./harness/web/pages/images.mjs";
import { h, fill, append } from "./harness/web/lib/dom.mjs";
import { Node, createDocument, storage, walk } from "./tests/web_stub_dom.mjs";
const { doc, byId } = createDocument();
globalThis.document = doc;
globalThis.Node = Node;
globalThis.localStorage = storage();
const timers = new Map();
let serial = 0;
globalThis.setTimeout = globalThis.setInterval = (fn) => { timers.set(++serial, fn); return serial; };
globalThis.clearTimeout = globalThis.clearInterval = (id) => timers.delete(id);
const tick = async () => { const [id, fn] = [...timers.entries()].at(-1); timers.delete(id); await fn(); };
let leave = [];
const clean = () => { for (const fn of leave) fn(); leave = []; fill(byId.app); };
let guest = false;
const status = { phase: "generating", progress: {value: 2, max: 4},
    modes: {fast: {label: "Fast", available: true}}, aspect_ratios: ["1:1"],
    resolutions: {standard: {label: "Standard", sizes: {"1:1": [1024, 1024]}}},
    edit: {enabled: true, available: true}, upscale: {available: true} };
let image = {id: "result", prompt: "A lake", model: "fast", status: "running",
    created_at: Date.now() / 1000, width: 1024, height: 1536, service: status};
let images = [image, {...image, id: "queued", status: "queued"},
    {...image, id: "failed", status: "failed"}, {...image, id: "done", status: "done"}];
const loaded = [];
const views = mountImages({ $app: byId.app, h, fill, append,
    api: async (path) => path === "/images" ? {images, status} : path === "/gpu"
        ? {state: "clear"} : image, setHeader() {}, toast() {}, go() {}, route: {},
    isGuest: () => guest, isMember: () => false, onLeave: (fn) => leave.push(fn),
    progressBar: () => h("div", {class: "progress"}, h("span")),
    confirmGpuQueue: async () => true, daemonImage: (path, attrs) => {
        loaded.push(path); return h("img", attrs); }, downloadDaemonFile() {}, location: {} });
const find = (name) => walk(byId.app, (el) => el.classList.contains(name))[0];
await views.viewImages();
assert(byId.app.classList.contains("images-page"));
assert.equal(find("image-generation").parentNode, find("images-workspace"));
assert.equal(find("image-gallery").parentNode, find("images-workspace"));
assert.equal(find("image-grid").parentNode, find("image-gallery"));
assert.deepEqual(walk(find("image-grid"), el => el.tagName === "A").map(el => el.href),
    ["#/images/result", "#/images/queued", "#/images/failed", "#/images/done"]);
assert.equal(loaded.length, 1); // unfinished jobs stay as tiles, never broken images
assert.equal(find("image-status-detail").textContent, "Sampling 50% · 2 / 4 steps");
const firstCard = find("image-card");
await tick();
assert.equal(find("image-card"), firstCard); // progress-only polls retain thumbnails
images = images.map(img => ({...img, status: "done"}));
await tick();
assert.notEqual(find("image-card"), firstCard);
assert.equal(walk(find("image-grid"), el => el.tagName === "IMG").length, 4);
clean();
assert.equal(timers.size, 0);
assert(!byId.app.classList.contains("images-page"));
await views.viewImage("result");
assert.equal(find("image-preview").parentNode, find("image-detail-layout"));
assert.equal(find("image-details").parentNode, find("image-detail-layout"));
assert.match(find("image-details").textContent, /Cancel/);
image = {...image, status: "done"};
await tick();
assert.equal(find("image-full").parentNode.href, "#/images/result/full");
assert.match(find("image-details").textContent, /Another one.*Edit.*Upscale.*Download.*Delete/);
clean();
assert.equal(timers.size, 0);
guest = true;
await views.viewImages();
assert.equal(walk(find("image-generation"), el => el.tagName === "FORM").length, 0);
assert.match(find("image-generation").textContent, /Demo access/);
assert(find("image-grid"));
clean();
await views.viewImageFull("result");
assert(!byId.app.classList.contains("images-page"));
assert(find("image-viewer"));
console.log("ok");
'''
    result = subprocess.run(
        [node, "--input-type=module"], input=script, cwd=root, capture_output=True,
        text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_image_desktop_rules_are_scoped_to_breakpoints():
    root = Path(__file__).resolve().parents[1]
    css = (root / "harness/web/style.css").read_text(encoding="utf-8")
    desktop = css.split("/* Desktop 7: Images (#568).", 1)[1].split("/* End Desktop 7. */", 1)[0]
    assert "@media (min-width: 768px)" in desktop
    assert "grid-template-columns: 380px minmax(0, 1fr)" in desktop
    assert "repeat(auto-fill, minmax(180px, 1fr))" in desktop
    assert "@media (min-width: 1280px)" in desktop
    assert "grid-template-columns: minmax(0, 1fr) 380px" in desktop
    assert "object-fit: contain" in desktop
    assert "var(--panel)" in desktop

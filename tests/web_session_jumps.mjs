// Jump-arrow visibility from scrollY / viewport / page height (#184).
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = readFileSync(join(root, "harness/web/app.js"), "utf8");
const match = src.match(/function sessionJumpHidden\([\s\S]*?\n\}/);
if (!match) throw new Error("sessionJumpHidden missing from app.js");
const sessionJumpHidden = new Function(`${match[0]}; return sessionJumpHidden;`)();

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

const desktopTop = sessionJumpHidden(0, 900, 1300);
assert(desktopTop.top === true, "jump-top hidden at top of desktop transcript");
assert(desktopTop.bottom === false, "jump-bottom shows at top when overflow exceeds 160px (was hidden at 0.75*900)");

const desktopBottom = sessionJumpHidden(400, 900, 1300);
assert(desktopBottom.top === false, "jump-top shows at bottom of desktop transcript");
assert(desktopBottom.bottom === true, "jump-bottom hidden at bottom");

const short = sessionJumpHidden(0, 900, 980);
assert(short.top === true && short.bottom === true, "both hidden when page barely taller than the window");

const iosTop = sessionJumpHidden(0, 700, 4000);
assert(iosTop.top === true && iosTop.bottom === false, "iOS long transcript shows jump-bottom at start");

const iosMid = sessionJumpHidden(2000, 700, 4000);
assert(iosMid.top === false && iosMid.bottom === false, "iOS mid-transcript shows both");

const iosBot = sessionJumpHidden(3300, 700, 4000);
assert(iosBot.top === false && iosBot.bottom === true, "iOS long transcript shows jump-top at end");

const tiny = sessionJumpHidden(0, 120, 800);
assert(tiny.far <= 0.75 * 120, "far still 0.75*view on very short viewports");

console.log("ok");

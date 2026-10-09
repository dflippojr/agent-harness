// Jump-button visibility rules. Pure: no DOM.

// 0.75*innerHeight on a tall desktop window is often larger than the whole
// overflow, so both arrows stay hidden unless the transcript is >1.75 viewports.
export function sessionJumpHidden(y, viewH, pageH) {
  const vh = Math.max(1, Number(viewH) || 0);
  const far = Math.min(160, 0.75 * vh);
  return { top: y <= far, bottom: pageH - vh - y <= far, far };
}

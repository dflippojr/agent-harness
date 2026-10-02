// Header and jump-button visibility rules. Pure: no DOM.

export function profileIconHidden(topLevel, page = false) {
  // Show on Chat, Agents, Tasks, Images, and Actions. Hide on nested Back pages
  // and on Profile (page: true). Guest chrome is unchanged; this flag is route-only.
  return !topLevel || !!page;
}

// 0.75*innerHeight on a tall desktop window is often larger than the whole
// overflow, so both arrows stay hidden unless the transcript is >1.75 viewports.
export function sessionJumpHidden(y, viewH, pageH) {
  const vh = Math.max(1, Number(viewH) || 0);
  const far = Math.min(160, 0.75 * vh);
  return { top: y <= far, bottom: pageH - vh - y <= far, far };
}

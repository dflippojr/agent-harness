// @ts-nocheck
// First-load chain (#258): /health and /me start together; /me is a read-only GET whose result is only adopted once /health
// passes. Resolves after the first route settles (or on any early exit/failure) and always dismisses the boot splash.
// Everything it touches is an argument, so this imports under plain Node.
export function startBoot({ window, checkCompatibility, fetchMe, adoptMe, paintGuestChrome, loadProfileIcon, readAppIcon, applyAppIcon, route }) {
  const bootCompatible = checkCompatibility();
  const bootIdentity = fetchMe();
  return bootCompatible.then((compatible) => (compatible ? bootIdentity : null)).then((me) => {
    if (!me) return null;
    adoptMe(me);
    paintGuestChrome();
    // The profile emoji paints when it arrives; route data never waits on it.
    void loadProfileIcon().then(() => applyAppIcon(readAppIcon()));
    applyAppIcon(readAppIcon());
    return route();
  }).finally(() => {
    // Includes compatibility/login early exits and failures; route paints its existing error state.
    // Removal is instant, with no minimum time or fade-out, even during the icon's fade-in.
    window.dismissBootSplash?.();
  });
}

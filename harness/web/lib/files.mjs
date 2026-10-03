// Daemon-served files (#258): images and downloads that need the owner's credentials. Browser globals arrive through
// `browser`, so importing this module touches nothing and works under plain Node.
import { h } from "./dom.mjs";

export function mountDaemonFiles({ agentHarnessWeb, isBlocked, ownerSurface, toast, browser }) {
  const { document } = browser;

  function daemonImage(path, attrs = {}) {
    const img = h("img", { ...attrs, alt: attrs.alt || "" });
    if (isBlocked()) return img;
    if (!agentHarnessWeb.token) {
      img.src = agentHarnessWeb.url(path, ownerSurface());
    } else {
      agentHarnessWeb.blob(path, "admin").then((blob) => {
        const url = URL.createObjectURL(blob);
        img.src = url;
        img.addEventListener("load", () => URL.revokeObjectURL(url), { once: true });
      }).catch((e) => { img.alt = `${attrs.alt || "Image"} (${e.message})`; });
    }
    return img;
  }

  async function downloadDaemonFile(path, filename) {
    if (isBlocked()) return;
    try {
      const blob = await agentHarnessWeb.blob(path, ownerSurface());
      const url = URL.createObjectURL(blob);
      const link = h("a", { href: url, download: filename });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { toast(e.message); }
  }

  return { daemonImage, downloadDaemonFile };
}

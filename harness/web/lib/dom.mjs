// @ts-nocheck
// DOM construction helpers (#258): h() builds an element, fill()/append() set children without the null/array pitfalls of the
// native calls. Browser globals (document, Node) are read when a helper runs, never at module top level, so this imports under
// plain Node; a test that calls them installs a stub document first.
const isEmptyChild = (c) => c === null || c === undefined || c === false;

function setAttr(el, k, v) {
  if (k === "class") el.className = v;
  else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
  else if (k === "html") el.innerHTML = v;
  else el.setAttribute(k, v === true ? "" : v);
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    setAttr(el, k, v);
  }
  for (const c of children.flat()) {
    if (isEmptyChild(c)) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Native append/replaceChildren stringify null as "null" and arrays via toString()
// (an anchor becomes its href, so a list of links becomes comma-joined URLs).
export function kids(...children) {
  return children.flat().filter((c) => c !== null && c !== undefined && c !== false);
}

export function fill(el, ...children) {
  el.replaceChildren(...kids(...children));
  return el;
}

export function append(el, ...children) {
  const nodes = kids(...children);
  if (nodes.length) el.append(...nodes);
  return el;
}

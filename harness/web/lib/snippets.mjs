// Chat snippet languages (#85). Pure: no DOM.

// Mirrors harness/snippets.py LANGUAGES; the server validates every run. A fence tag only decides whether a block
// gets a Run button, and the button names the language it runs as. Nothing runs unless the owner clicks.
export const SNIPPET_LANGUAGES = {
  python: { label: "Python", aliases: ["python", "py", "python3"] },
  javascript: { label: "JavaScript", aliases: ["javascript", "js", "node", "mjs", "cjs"] },
  java: { label: "Java", aliases: ["java"] },
  csharp: { label: "C#", aliases: ["csharp", "cs", "c#"] },
  cpp: { label: "C++", aliases: ["cpp", "c++", "cxx", "cc"] },
};

export function snippetLanguage(tag) {
  const t = String(tag || "").trim().toLowerCase();
  return Object.keys(SNIPPET_LANGUAGES).find((id) => SNIPPET_LANGUAGES[id].aliases.includes(t)) || "";
}

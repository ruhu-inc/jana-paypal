// INP-13: clipboard HTML becomes plain text and a small MathML subset; no HTML is inserted into the editor.
import { normaliseInput, INPUT_LIMITS, InputError } from "@jana/client-core";

function mathml(node: Element, depth = 0): string {
  if (depth > 64) throw new InputError("too_deep", 0);
  const children = [...node.children].map((child) => mathml(child, depth + 1));
  const name = node.localName.toLowerCase();
  if (["mi", "mn", "mo", "mtext"].includes(name)) return normaliseInput(node.textContent ?? "");
  if (name === "mfrac" && children.length === 2) return `frac(${children[0]},${children[1]})`;
  if (name === "msup" && children.length === 2) return `(${children[0]})^(${children[1]})`;
  if (name === "msub" && children.length === 2) return `${children[0]}_(${children[1]})`;
  if (name === "msqrt") return `sqrt(${children.join("")})`;
  if (name === "mroot" && children.length === 2) return `root(${children[1]},${children[0]})`;
  if (name === "mfenced") return `(${children.join(",")})`;
  if (["math", "mrow", "mstyle", "semantics"].includes(name)) return children.join("");
  return "";
}

export function clipboardText(data: Pick<DataTransfer, "getData">): string {
  const plain = data.getData("text/plain");
  const xml = data.getData("text/mathml");
  const html = data.getData("text/html");
  let text = plain;
  if (xml !== "" || html !== "") {
    const raw = xml || html;
    if (raw.length > 256000) throw new InputError("too_long", INPUT_LIMITS.text);
    // Parse in an inert template: DOMParser documents can start image/iframe fetches before we drop those nodes.
    const template = document.createElement("template");
    template.innerHTML = raw;
    const walk = (node: Node): string => {
      if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
      if (!(node instanceof Element)) return "";
      const name = node.localName.toLowerCase();
      if (["script", "style", "img", "iframe", "object", "svg", "annotation"].includes(name)) return "";
      if (name === "math") return `\\(${mathml(node)}\\)`;
      if (name === "br") return "\n";
      if (name === "ol") {
        const start = node.getAttribute("start") ?? "1";
        let number = /^[1-9]\d?$/.test(start) ? Number(start) : 1;
        return [...node.children].map((child) => child.localName.toLowerCase() === "li"
          ? `${number++}. ${walk(child).trimEnd()}\n` : walk(child)).join("");
      }
      const content = [...node.childNodes].map(walk).join("");
      return ["p", "div", "li", "tr"].includes(name) ? `${content}\n` : content;
    };
    text = [...template.content.childNodes].map(walk).join("").trimEnd();
  }
  text = normaliseInput(text);
  if (text.length > INPUT_LIMITS.text) throw new InputError("too_long", INPUT_LIMITS.text);
  return text;
}

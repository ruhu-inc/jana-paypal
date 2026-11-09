// Splitting streamed text into markdown blocks (UIA-13): settled blocks never change again, so they render once and
// keep their DOM nodes; only the tail block is re-parsed on each flush. A blank line ends a block, except inside a
// code fence or display maths, which end only at their closing delimiter.

export interface Blocks {
  settled: string[];
  tail: string;
}

function opens(line: string, inFence: boolean, inMath: boolean): { fence: boolean; math: boolean } {
  const trimmed = line.trim();
  let fence = inFence;
  let math = inMath;
  if (!math && /^(```|~~~)/.test(trimmed)) fence = !fence;
  if (!fence) {
    if (trimmed === "$$" || trimmed === "\\[") math = true;
    if (trimmed === "$$" && inMath) math = false;
    if (trimmed === "\\]") math = false;
    if (!inMath && /^\$\$.+\$\$$/.test(trimmed)) math = false;
  }
  return { fence, math };
}

/** Split `text` into blocks. With `done`, every block is settled; otherwise the last block is the live tail. */
export function splitBlocks(text: string, done: boolean): Blocks {
  const blocks: string[] = [];
  let current: string[] = [];
  let fence = false;
  let math = false;
  const lines = text.split("\n");
  lines.forEach((line, i) => {
    const last = i === lines.length - 1;
    if (line.trim() === "" && !fence && !math) {
      if (current.length > 0) {
        blocks.push(current.join("\n"));
        current = [];
      }
      return;
    }
    const next = opens(line, fence, math);
    fence = next.fence;
    math = next.math;
    current.push(line);
    if (last) blocks.push(current.join("\n"));
  });
  if (done) return { settled: blocks, tail: "" };
  // Text that ends at a blank line has no live block yet; otherwise the last block may still grow.
  if (text.endsWith("\n\n") && !fence && !math) return { settled: blocks, tail: "" };
  const tail = blocks.pop() ?? "";
  return { settled: blocks, tail };
}

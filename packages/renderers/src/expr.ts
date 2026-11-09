// A small, safe expression evaluator for plotting (no eval, no property access): numbers, one variable, + - * / ^,
// unary minus, parentheses, implicit multiplication (2x, 3(x+1)), and a fixed set of functions and constants.
type Node =
  | { k: "num"; v: number }
  | { k: "var" }
  | { k: "neg"; a: Node }
  | { k: "bin"; op: string; a: Node; b: Node }
  | { k: "fn"; name: string; a: Node };

const FUNCTIONS: Record<string, (x: number) => number> = {
  sin: Math.sin, cos: Math.cos, tan: Math.tan, asin: Math.asin, acos: Math.acos, atan: Math.atan,
  sqrt: Math.sqrt, abs: Math.abs, exp: Math.exp, ln: Math.log, log: Math.log10,
};
const CONSTANTS: Record<string, number> = { pi: Math.PI, e: Math.E };

export class ExprError extends Error {}

function tokenize(src: string): string[] {
  const tokens = src.replace(/\s+/g, "").replace(/\*\*/g, "^").match(/\d+\.?\d*(?:e[+-]?\d+)?|\.\d+|[a-z]+|[-+*/^()]/gi);
  if (tokens === null || tokens.join("") !== src.replace(/\s+/g, "").replace(/\*\*/g, "^")) throw new ExprError("unexpected character");
  return tokens;
}

export function parse(src: string, variable = "x"): Node {
  const tokens = tokenize(src);
  let i = 0;
  const peek = () => tokens[i];
  const take = () => tokens[i++];
  const startsFactor = (t: string | undefined) => t !== undefined && (/^[\d.]/.test(t) || /^[a-z]/i.test(t) || t === "(");

  const primary = (): Node => {
    const t = take();
    if (t === undefined) throw new ExprError("unexpected end");
    if (/^[\d.]/.test(t)) return { k: "num", v: Number(t) };
    if (t === "(") {
      const inner = sum();
      if (take() !== ")") throw new ExprError("missing )");
      return inner;
    }
    if (/^[a-z]+$/i.test(t)) {
      if (t === variable) return { k: "var" };
      const constant = CONSTANTS[t];
      if (constant !== undefined) return { k: "num", v: constant };
      if (t in FUNCTIONS) {
        if (take() !== "(") throw new ExprError("function needs (");
        const arg = sum();
        if (take() !== ")") throw new ExprError("missing )");
        return { k: "fn", name: t, a: arg };
      }
      throw new ExprError(`unknown name ${t}`);
    }
    throw new ExprError(`unexpected ${t}`);
  };
  const power = (): Node => {
    const base = primary();
    if (peek() === "^") {
      take();
      return { k: "bin", op: "^", a: base, b: unary() };
    }
    return base;
  };
  const unary = (): Node => {
    if (peek() === "-") {
      take();
      return { k: "neg", a: unary() };
    }
    if (peek() === "+") take();
    return power();
  };
  const product = (): Node => {
    let left = unary();
    for (;;) {
      const t = peek();
      if (t === "*" || t === "/") {
        take();
        left = { k: "bin", op: t, a: left, b: unary() };
      } else if (startsFactor(t)) {
        left = { k: "bin", op: "*", a: left, b: power() };
      } else return left;
    }
  };
  const sum = (): Node => {
    let left = product();
    while (peek() === "+" || peek() === "-") {
      const op = take() ?? "+";
      left = { k: "bin", op, a: left, b: product() };
    }
    return left;
  };
  const tree = sum();
  if (i !== tokens.length) throw new ExprError("trailing input");
  return tree;
}

export function evaluate(node: Node, x: number): number {
  switch (node.k) {
    case "num":
      return node.v;
    case "var":
      return x;
    case "neg":
      return -evaluate(node.a, x);
    case "fn": {
      const fn = FUNCTIONS[node.name];
      return fn === undefined ? Number.NaN : fn(evaluate(node.a, x));
    }
    case "bin": {
      const a = evaluate(node.a, x);
      const b = evaluate(node.b, x);
      return node.op === "+" ? a + b : node.op === "-" ? a - b : node.op === "*" ? a * b : node.op === "/" ? a / b : a ** b;
    }
  }
}

/** Sample `expr` over [from, to]; non-finite values split the curve into segments. */
export function sample(expr: string, from: number, to: number, n = 200, variable = "x"): [number, number][][] {
  const tree = parse(expr, variable);
  const segments: [number, number][][] = [];
  let current: [number, number][] = [];
  for (let k = 0; k <= n; k += 1) {
    const x = from + ((to - from) * k) / n;
    const y = evaluate(tree, x);
    if (Number.isFinite(y)) current.push([x, y]);
    else if (current.length > 0) {
      segments.push(current);
      current = [];
    }
  }
  if (current.length > 0) segments.push(current);
  return segments;
}

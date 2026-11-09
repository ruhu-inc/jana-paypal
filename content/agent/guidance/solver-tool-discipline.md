---
id: solver-tool-discipline
version: "0.2"
kind: technique
applies_to: [solver, checker]
models: ["*"]
requires_tools: [math_solve]
priority: 70
max_chars: 700
owner: Engine lead
status: draft
---
Batch independent tool calls in one round.
Solve equations with `math_solve`; never rearrange in your head.
Compare two forms with `math_check_equivalence` instead of judging by eye.
When offered, use `math_simplify` for symbolic simplification; copy its whole expression into the final value.
Checking an exponent alone does not verify a symbolic answer.
Never repeat an unchanged failing call: read the error and fix the arguments.

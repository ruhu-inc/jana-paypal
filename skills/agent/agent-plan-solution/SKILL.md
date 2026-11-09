---
name: agent-plan-solution
kind: agent
version: "0.1"
status: draft
language: en
summary: Plan a solution in which every value is computed by a tool
description: "Solve the problem for the team: plan the steps of a hidden solution, compute every value with a tool and keep the check ids. Not for: talking to the student (the Coach does that) or checking a plan (use agent-check-plan)."
subject: general
audience: team
roles: [solver]
requires_tools: [math_solve, math_evaluate, units_convert, units_check_dimensions]
owner: engine-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-09"
success_criteria:
  - Every value in the plan carries the check id of the tool call that produced it
  - The steps are in the order a student would write them
---

# Plan a solution

## When to use

Use once per problem, after the diagnosis, to build the plan the Coach teaches from.

Not for: writing to the student or marking the student's lines.

## What this skill does not do

- It does not compute in its head: every value comes from a tool result.
- It does not skip unit conversion.
- It does not show the plan to the student.

## Inputs

- The question and the diagnosis (knowns, unknowns, topic).
- The topic's teaching skill, when there is one.

## Procedure

1. Choose the method the teaching skill names for this kind of problem. Done when the method is one sentence.
2. Convert every known to consistent units with units_convert. Done when each converted value has a check id.
3. Write the steps in the order a student would write them. Done when each step is one equation or one substitution.
4. Compute each step with math_solve or math_evaluate. Done when every step has a value and a check id.
5. Check the final unit with units_check_dimensions. Done when the dimension matches the unknown.

## Output contract

- steps: equation, value, unit and check id for each step.
- answer: the final value, unit and check id.
- method: the one-sentence method.

## Pitfalls

- Use the constants the question gives before any default, because exam boards differ.
- Keep exact values until the last step, since early rounding drifts.

## Verification checklist

- Every value has a check id from this session.
- The final unit passed the dimension check.
- No step is missing between the knowns and the answer.

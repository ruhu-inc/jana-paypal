---
name: agent-check-plan
kind: agent
version: "0.1"
status: draft
language: en
summary: Check a solution plan or a student line by independent re-derivation
description: "Check a plan, mark a line of working or verify an answer: re-derive each step with the checking tools and report a verdict per step. Not for: building the plan (use agent-plan-solution) or explaining to the student."
subject: general
audience: team
roles: [checker]
requires_tools: [math_check_step, math_check_equivalence, units_check_dimensions]
owner: engine-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-09"
success_criteria:
  - Every step has a verdict backed by a check id
  - An invalid step names its error category
---

# Check a plan or a line

## When to use

Use for every Solver plan before the Coach sees it, and for every line or answer the student
submits.

Not for: building a plan or talking to the student.

## What this skill does not do

- It does not read the Solver's reasons, only its claimed steps.
- It does not repair a wrong step: it reports it.
- It does not round before comparing.

## Inputs

- The problem and its knowns.
- The claimed steps and answer, or the student's line and the line before it.

## Procedure

1. Check each claimed step against the one before with math_check_step. Done when every step has a verdict.
2. Check the final answer against the question with math_check_equivalence. Done when the answer has a verdict.
3. Check the unit of the answer with units_check_dimensions. Done when the dimension matches.
4. Report the first invalid step and its category. Done when the verdict lists it, or every step is valid.

## Output contract

- verdict: correct, incorrect or unknown, overall.
- steps: per step, the verdict, the category when invalid and the check id.

## Pitfalls

- Treat unknown as unknown, never as correct, because an undecided check proves nothing.
- Compare against the question's own constants, since a pack default may differ.

## Verification checklist

- Every verdict has a check id.
- Unknown verdicts are reported as unknown.
- The Solver's reasons were not used.

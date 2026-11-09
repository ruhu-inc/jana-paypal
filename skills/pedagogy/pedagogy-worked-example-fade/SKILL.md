---
name: pedagogy-worked-example-fade
kind: pedagogy
version: "0.2"
status: draft
language: en
summary: Show a checked worked example of a similar problem, then fade the support
description: "Show me an example, worked example, how do I start, after repeated failure: the tutor solves a similar problem with checked values, then the student does a variant with fewer steps shown. Not for: the student's own problem (use pedagogy-guided-solve) or a first look at a new idea."
subject: general
audience: student
roles: [coach]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
owner: learning-designer
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-09"
success_criteria:
  - The student explains why each step of the example follows from the one before
  - The student completes a similar problem with fewer steps shown
---

# Worked example, then fade

## When to use

Use when the student has failed the same step several times, asks to see an example, or does
not know how to start a type of problem.

Not for: the student's own problem, whose answer stays withheld (use pedagogy-guided-solve),
or a topic the student has never met.

## What this skill does not do

- It does not use the student's own problem as the example.
- It does not show values without checks.
- It does not skip the faded practice.

## Objectives

1. The student explains why each step of the example follows from the one before.
2. The student completes a similar problem with fewer steps shown.

## Checked board dialogue

For a checked-board reply, use the engine's current phase instead of starting a demonstration or a variant.
Ask about the current mistaken operation without solving the learner's problem or introducing new numbers.
When the final working is checked and correct, confirm completion and invite a new question. Resume fading
only when the engine selects that teaching activity; do not turn a completed calculation into more work.

## Method

1. Pick a problem of the same kind with different numbers, solved by the Solver and checked. Done when every value in it has a check.
2. Show the example one step at a time and ask why each step follows. Done when the student has given a reason for each step.
3. Give a similar problem with the first step shown. Done when the student has finished it.
4. Give another with no steps shown. Done when the student has finished it or asked for a hint.
5. Return to the student's own problem. Done when the student has started it.

## Branches by student state

| State | Signal | Move |
| --- | --- | --- |
| Right and reasoned | Reasons match the steps | Remove one more step of support |
| Right but sloppy | Copies the steps, cannot say why | Ask why before moving on |
| Wrong | Faded problem goes wrong at one step | Point back to that step of the example |
| Guessing | Changes the method between steps | Ask which step of the example this is |
| Stuck | Cannot start the faded problem | Show its first step, then fade again |

## Confidence and escalation

- If the faded problem fails twice, show a second example that differs in one feature only.
- If the student is upset, pause and follow the safety guidance.

## Worked exchange

> Tutor: Here is a similar one: a bike goes from 2 m/s to 8 m/s in 3 s. Its acceleration is 2 m/s² (checked). Why do we subtract 2 from 8 first?
> Student: Because a is the change in speed over the time.
> Tutor: Exactly. Now you try: a runner goes from 1 m/s to 7 m/s in 2 s. What is the change in speed?
> Student: 6 m/s.
> Tutor: That's right (checked). What do you divide it by?

## Verification checklist

- The example is not the student's own problem.
- Every value shown in the example has a check.
- The faded problems were done by the student.

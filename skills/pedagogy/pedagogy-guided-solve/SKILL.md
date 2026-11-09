---
name: pedagogy-guided-solve
kind: pedagogy
version: "0.2"
status: draft
language: en
summary: Guide a student through a problem one checked step at a time
description: "Help me solve this, guide me, step by step, I'm stuck on a question: the student does each step, the tutor asks, hints one rung at a time and confirms only checked values. Not for: a quick definition (answer directly) or a quiz on a known topic (use pedagogy-worked-example-fade)."
subject: general
audience: student
roles: [coach]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
  min_rung: 1
escalation:
  after_failed_attempts: 3
  action: worked_example
owner: learning-designer
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-09"
success_criteria:
  - The student writes every step of the solution on the board themselves
  - The student names the knowns and the unknown before choosing a method
  - Every value the tutor confirms comes from a tool check
---

# Guided solve

## When to use

Use when the student brings a problem and wants to solve it, or says they are stuck on it.
The student does the work; the tutor asks, hints and confirms.

Not for: a quick definition or fact (answer it in one line) or drilling a topic the student
already knows (use pedagogy-worked-example-fade or a drill).

## What this skill does not do

- It does not solve the problem for the student.
- It does not give a later step before the current one is done.
- It does not confirm a value that has no check behind it.
- It does not choose the hint rung: the engine sets it.

## Objectives

1. The student writes every step of the solution on the board themselves.
2. The student names the knowns and the unknown before choosing a method.
3. Every value the tutor confirms comes from a tool check.

## Checked board dialogue

The engine has already selected this phase. With Given and Find ready, ask how the supplied quantities relate;
do not restart the knowns or unknowns. On a checked error, ask about that operation and wait for the learner.
On a checked final answer, acknowledge completion and invite a new question. Do not request another unit,
sense check or unfinished step that the engine already accepted. Later learner requests can reopen explanation.

## Method

1. Restate the goal in one sentence and ask what the question gives. Done when the student has listed at least one known.
2. Ask for the unknown and its unit. Done when the unknown is on the board.
3. Ask which rule or equation links the knowns to the unknown. Done when the student names one.
4. Ask the student to do the current step. Done when a line is on the board.
5. Use the Checker's verdict on that line and the branches below. Done when the reply matches exactly one branch.
6. When a hint is due, give the rung the engine set and nothing beyond it. Done when the hint is shown and you are waiting.
7. At the final answer, ask for the unit and a sense check. Done when the student has stated both.

## Branches by student state

| State | Signal | Move |
| --- | --- | --- |
| Right and reasoned | Verdict valid; reason given | Confirm in a few words; move to the next step |
| Right but sloppy | Value right; unit, sign or reason missing | Ask for the missing piece before moving on |
| Wrong | Verdict invalid with a category | Ask one question aimed at the faulty step |
| Guessing | Answers change fast; no working shown | Ask for the rule before any value |
| Stuck | No attempt, or two failed attempts | Ask an easier entry question, then the engine's rung |

## Confidence and escalation

- After three failed attempts on one step, switch to a worked example of a similar problem.
- If the student is upset, pause the problem and follow the safety guidance.
- If a misconception keeps returning, note it for the session summary.

## Worked exchange

> Tutor: A car speeds up from 10 m/s to 30 m/s in 4 s. What is its acceleration? What do you know?
> Student: u is 10, v is 30 and t is 4.
> Tutor: Good. Which equation links u, v, t and a?
> Student: v = u + at.
> Tutor: Put the values in and solve for a.
> Student: 30 = 10 + 4a, so a = 5.
> Tutor: That's right (checked). What is the unit of a?

## Verification checklist

- The student wrote each step; you only asked and hinted.
- Every value you confirmed had a check.
- Each message asked one question.
- No later step or final answer appeared early.

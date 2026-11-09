---
name: agent-diagnose-question
kind: agent
version: "0.1"
status: draft
language: en
summary: Classify a question and extract its knowns, unknowns and topic
description: "Diagnose a question or a student answer: classify subject and kind, extract knowns with units and the unknown, and separate evidence from inference. Not for: solving the problem (use agent-plan-solution) or marking working (use agent-check-plan)."
subject: general
audience: team
roles: [diagnoser]
owner: engine-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-09"
success_criteria:
  - Every known has a value, a unit and a quote from the question
  - The unknown is named with its expected unit
---

# Diagnose a question

## When to use

Use once per new problem, and again after a wrong final answer.

Not for: building the solution plan or marking a line of working.

## What this skill does not do

- It does not solve or hint.
- It does not guess a value the question does not state.
- It does not name a misconception without evidence from the student's input.

## Inputs

- The question text.
- Numbered candidate knowledge components and misconceptions.
- The student's latest answer, when there is one.

## Procedure

1. Decide whether the question is in scope for the session's subject and level. Done when in_scope is set.
2. Quote each given value with its unit exactly as written. Done when every number in the question appears once in knowns.
3. Name the unknown and its unit. Done when unknowns has one entry or the question asks for several.
4. Pick the candidate knowledge components by number. Done when each choice cites a phrase of the question.
5. If there is a student answer, compare it with the knowns and name the first faulty step as evidence. Done when evidence and inference are separate fields.

## Output contract

- in_scope, subject, topic_ref, kc_refs: classification.
- knowns: symbol, value, unit and the quote it came from.
- unknowns: what is asked, with units.
- confidence: how sure the classification is.

## Pitfalls

- Never convert units here: conversion is a tool check later.
- Never infer a value from common knowledge, because the question or the pack sets constants.

## Verification checklist

- Every number in knowns appears in the question text.
- No field holds a computed value.
- Evidence quotes the student's input; inference is labelled as inference.

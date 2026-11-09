---
name: chm-o-states
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach the particle model, changes of state and diffusion of gases
description: "States of matter, particle model, melting point, boiling point, diffusion: picture the particles, compare the temperature with both fixed points, and link diffusion speed to molecular mass and temperature. Not for: bonding and structure (chm-o-atoms)."
subject: chemistry
levels: [O]
topics: [states of matter, particle model, melting point, boiling point, diffusion]
kcs:
  - kc.chm.o.states.particle_model
  - kc.chm.o.states.changes_of_state
  - kc.chm.o.states.diffusion
curriculum_refs: [CHM-O-01.01, CHM-O-01.02]
prerequisites: []
related: [chm-o-atoms]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [math_evaluate]
visuals: [particle_diagram]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: switch_representation
plausibility:
  - quantity: temperature
    unit: degC
    min: -273
    max: 3700
    context: melting and boiling points of substances met at O level, from helium to tungsten
  - quantity: relative_molecular_mass
    unit: dimensionless
    min: 2
    max: 300
    context: gases compared in diffusion questions, from hydrogen to iodine vapour
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student describes the arrangement, spacing and motion of particles in each state
  - The student decides the state at a given temperature by comparing it with both the melting point and the boiling point
  - The student predicts which gas diffuses faster from relative molecular mass and temperature, with a particle reason
---

# States of matter and diffusion

## When to use

Use when a question asks how particles are arranged or move in a solid, a liquid or a gas, what state a substance is
in at a stated temperature, what change of state happens on heating or cooling, or which gas spreads out faster.

Not for: why a substance has a high or low melting point (bonding and structure, chm-o-atoms).

## What this skill does not do

- It does not explain melting points through bonding; it only reads them.
- It does not use gas laws or calculate rates of diffusion.
- It does not treat mixtures that melt over a range, except to say an impure sample does so.

## Objectives

1. The student describes the arrangement, spacing and motion of particles in each state.
2. The student decides the state at a given temperature by comparing it with both the melting point and the boiling point.
3. The student predicts which gas diffuses faster from relative molecular mass and temperature, with a particle reason.

## Prerequisite probe

- "Put these temperatures in order from coldest to hottest: 25 °C, −10 °C, 0 °C." Expected: −10 °C, 0 °C, 25 °C.

If it fails, draw a vertical thermometer scale with zero marked, then return.

## Teaching sequence

1. Ask the student to describe one state at a time: arrangement, spacing, motion. Done when all three states are described with the three words.
2. Ask what happens to the particles on heating: they move faster and spread out; the particles themselves do not grow. Done when the student says the spacing changes, not the particle size.
3. For a state question, ask the student to mark the melting point and the boiling point on a temperature line. Done when both fixed points are on the line in the right order.
4. Ask where the given temperature sits: below the melting point is solid, between the two is liquid, above the boiling point is gas. Done when the student names the state and the fixed point that decides it.
5. For diffusion, ask which gas has the lower relative molecular mass; if the question gives no values, the math_evaluate result from the pack's relative atomic masses is the reference value. Done when the student names the lighter gas as the faster one.
6. Ask how a rise in temperature changes diffusion. Done when the student says the particles move faster, so diffusion is faster.

## Hint ladder

1. Orient: "Which state, or which gas, is the question about?"
2. Concept: "A substance melts at its melting point and boils at its boiling point; between the two it is a liquid."
3. First step: draw the temperature line with both fixed points and ask where the given temperature falls.
4. Worked step: place the temperature on the line and ask the student to name the region.
5. Full step: state the answer with the reason; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.states_particles_expand`: says solids expand because each particle swells → "When a metal bar warms up, is it the particles that get bigger, or the gaps between them?"
- `mc.chm.o.states_solid_particles_still`: says solid particles do not move at all → "If the particles in ice were completely still, how could heating ever make them break free?"
- `mc.chm.o.states_mp_bp_swapped`: uses the boiling point to decide melting → "Which fixed point tells you when the solid turns into a liquid?"
- `mc.chm.o.states_above_mp_gas`: calls anything above its melting point a gas → "What state is water at 50 °C, which is above 0 °C but below 100 °C?"
- `mc.chm.o.states_heavier_faster`: thinks heavier molecules diffuse faster → "At the same temperature, which moves faster: a light particle or a heavy one with the same energy?"
- `mc.chm.o.states_diffusion_needs_mixing`: thinks gases only mix when stirred → "How does a smell reach the back of a still room if nobody fans it?"
- `mc.chm.o.states_colder_faster`: thinks diffusion is faster when cold → "Do particles move faster or slower when the gas is cooled?"

## Confidence and escalation

- Two wrong states in a row: switch to a drawn temperature line with the three regions shaded.
- Repeated confusion about motion: switch to a particle diagram for each state.
- A question about a mixture or an unusual substance outside the pack: save it as a local question for human help.

## Worked exchange

> Tutor: A substance melts at −7 °C and boils at 59 °C. What state is it in at 25 °C, and at 80 °C?
> Student: At 25 °C it is above the melting point, so it is a gas.
> Tutor: Is 25 °C above or below the boiling point of 59 °C?
> Student: Below it. So it has melted but not boiled: it is a liquid.
> Tutor: Good. And at 80 °C?
> Student: 80 °C is above 59 °C, so it is a gas.

## Verification checklist

- Each state answer names the fixed point that decides it.
- Melting point and boiling point were not swapped when read from the question or a table.
- Any relative molecular mass used came from the question or a math_evaluate check with the pack's values.
- Diffusion answers give a particle reason: lighter or hotter particles move faster on average.

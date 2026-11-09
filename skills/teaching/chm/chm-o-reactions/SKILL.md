---
name: chm-o-reactions
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach chemical change, rates, equilibrium and redox at O level
description: "Rate of reaction, collision theory, equilibrium, redox, oxidation numbers, physical and chemical change: name the factor, explain with collisions, oppose the change at equilibrium, track electrons and oxidation numbers. Not for: energy profiles and ΔH (chm-o-energetics)."
subject: chemistry
levels: [O]
topics: [rate of reaction, collision theory, equilibrium, redox, oxidation numbers, chemical change]
kcs:
  - kc.chm.o.reactions.physical_chemical_change
  - kc.chm.o.reactions.rate_factors
  - kc.chm.o.reactions.collision_theory
  - kc.chm.o.reactions.rate_data
  - kc.chm.o.reactions.equilibrium
  - kc.chm.o.reactions.redox
  - kc.chm.o.reactions.oxidation_numbers
curriculum_refs: [CHM-O-06.01, CHM-O-06.02, CHM-O-06.03, CHM-O-06.04]
prerequisites:
  - kc.chm.o.atoms.ion_formation
related: [chm-o-energetics, chm-o-atoms]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [math_evaluate, chem_stoichiometry]
visuals: [energy_profile, particle_diagram, library_diagram]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: switch_representation
plausibility:
  - quantity: rate_by_volume
    unit: cm^3/s
    min: 0.001
    max: 50
    context: mean rate of gas production collected in a gas syringe or measuring cylinder
  - quantity: rate_by_mass
    unit: g/s
    min: 0.00001
    max: 1
    context: mean rate of mass loss of a flask on a balance
  - quantity: volume
    unit: cm^3
    min: 0
    max: 500
    context: gas volume collected in a school rate experiment
  - quantity: mass
    unit: g
    min: 0
    max: 500
    context: balance readings or mass lost in a school rate experiment
  - quantity: time
    unit: s
    min: 1
    max: 7200
    context: time intervals in a school rate experiment
  - quantity: oxidation_number
    unit: dimensionless
    min: -4
    max: 7
    context: oxidation numbers of elements in compounds and ions at O level
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student tells a physical change from a chemical change by whether a new substance forms
  - The student predicts and explains a change in rate using the frequency and energy of collisions
  - The student calculates a mean rate over an interval and predicts the shift of an equilibrium
  - The student identifies oxidation and reduction from electrons or oxidation numbers and names the agents
---

# Rates, equilibrium and redox

## When to use

Use when a question asks whether a change is physical or chemical, how a factor changes the rate and why, for a mean
rate from gas volume or mass readings, which way an equilibrium shifts, or which substance is oxidised or reduced and
what an oxidation number is.

Not for: enthalpy changes or reading ΔH from a profile (chm-o-energetics).

## What this skill does not do

- It does not use rate equations, orders or equilibrium constants.
- It does not measure a rate from a tangent unless the question gives the line.
- It does not decide industrial compromises beyond the direction of the shift and the speed.

## Objectives

1. The student tells a physical change from a chemical change by whether a new substance forms.
2. The student predicts and explains a change in rate using the frequency and energy of collisions.
3. The student calculates a mean rate over an interval and predicts the shift of an equilibrium.
4. The student identifies oxidation and reduction from electrons or oxidation numbers and names the agents.

## Prerequisite probe

- "What charge does a magnesium ion carry, and does the atom gain or lose electrons to form it?" Expected: 2+, it loses two electrons.

If it fails, detour to ion formation (chm-o-atoms), then return.

## Teaching sequence

1. For a change, ask whether a new substance forms. Done when the student classifies it and names the evidence.
2. For a rate factor, ask whether it makes collisions more frequent, more energetic, or lowers the activation energy. Done when the student names the factor's effect on collisions and says the total product is unchanged.
3. For rate data, ask for the change in reading and the length of the interval, then change ÷ time; the math_evaluate result is the reference value. Done when the student's mean rate matches the checked value with its unit.
4. For an equilibrium, ask what was changed and which direction opposes it: fewer gas moles for higher pressure, the endothermic side for heating. Done when the student names the direction and the reason, and says a catalyst does not shift it.
5. For redox, ask which species loses electrons (oxidised) and which gains them (reduced). Done when both are named and each agent is the other substance.
6. For an oxidation number, ask for the sum of the known values plus x to equal the overall charge; math_evaluate gives the reference value. Done when the student's value with its sign matches the checked value.

## Hint ladder

1. Orient: "Is the question about what forms, how fast, which way, or who gains electrons?"
2. Concept: name the idea, such as "a reaction needs collisions with at least the activation energy".
3. First step: write the relation or the electron change and ask the student to fill it in.
4. Worked step: show the change in reading or the sum of oxidation numbers and ask for the last step.
5. Full step: show the checked answer with its reason; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.reactions_state_change_is_chemical`: calls melting ice a chemical change → "Is the liquid that runs off melting ice a new substance?"
- `mc.chm.o.reactions_dissolving_is_chemical`: says dissolved sugar is destroyed → "If you evaporate the water, what is left in the dish?"
- `mc.chm.o.reactions_bigger_pieces_faster`: says lumps react faster than powder → "Which has more surface touching the acid: one lump or the same mass as powder?"
- `mc.chm.o.reactions_rate_changes_amount`: says heating gives more gas → "If the same mass of marble is used up, can a faster reaction make more gas?"
- `mc.chm.o.reactions_all_collisions_react`: thinks every collision reacts → "If every collision worked, how long would any reaction take?"
- `mc.chm.o.reactions_catalyst_gives_energy`: says a catalyst gives particles energy → "Does a catalyst make particles faster, or give them an easier route?"
- `mc.chm.o.reactions_rate_reading_not_change`: divides the end reading by the time → "What did the syringe read at the start of the interval?"
- `mc.chm.o.reactions_rate_inverted`: works out time ÷ volume → "A faster reaction should give a bigger rate. Does yours?"
- `mc.chm.o.reactions_shift_reinforces_change`: shifts towards more gas moles when pressure rises → "Which side would lower the pressure again?"
- `mc.chm.o.reactions_catalyst_shifts_equilibrium`: thinks a catalyst raises the yield → "A catalyst speeds both directions. Does it favour either one?"
- `mc.chm.o.reactions_oxidising_agent_is_oxidised`: calls the oxidised substance the oxidising agent → "The oxidising agent takes electrons. Does it end up oxidised or reduced?"
- `mc.chm.o.reactions_oxnum_charge_ignored`: makes oxidation numbers in an ion add to zero → "What overall charge does the sulfate ion carry?"

## Confidence and escalation

- Two failures on collision explanations: switch to a particle diagram before and after the change.
- Two failures on equilibrium: switch to the question "what would undo the change?".
- Three failures on electron transfer: detour to ion formation, then return.

## Worked exchange

> Tutor: A gas syringe reads 24 cm³ at 20 s and 54 cm³ at 50 s. What is the mean rate between 20 s and 50 s?
> Student: 54 ÷ 30, so 1.8 cm³/s.
> Tutor: What did the syringe read at the start of the interval?
> Student: 24 cm³. So the gas made in the interval is 54 − 24 = 30 cm³.
> Tutor: Good. And the length of the interval?
> Student: 50 − 20 = 30 s, so the rate is 30 ÷ 30 = 1.0 cm³/s.
> Tutor: Correct (checked with the maths tool).

## Verification checklist

- Every rate and oxidation number matches a math_evaluate result.
- A mean rate uses the change in reading over the length of the interval, with unit cm³/s or g/s.
- Oxidation numbers in an ion add to its charge, and each is counted by its subscript.
- Equilibrium answers name a direction and the change it opposes; a catalyst never shifts it.
- The total amount of product changes only with the limiting reactant, never with rate.

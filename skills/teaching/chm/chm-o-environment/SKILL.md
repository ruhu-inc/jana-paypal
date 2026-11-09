---
name: chm-o-environment
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach water tests and treatment, fertiliser nitrogen, air pollution and climate
description: "water treatment, fertilisers, air pollution, climate change, greenhouse gases: link each pollutant to its source, effect and remedy, and compare the nitrogen in fertilisers by percentage. Not for: general percentage composition (chm-o-stoichiometry) or metal corrosion (chm-o-metals)."
subject: chemistry
levels: [O]
topics: [environment, water, water treatment, fertilisers, air pollution, climate change, greenhouse gases]
kcs:
  - kc.chm.o.environment.water
  - kc.chm.o.environment.fertilisers
  - kc.chm.o.environment.air_pollution
  - kc.chm.o.environment.climate
curriculum_refs: [CHM-O-10.01, CHM-O-10.02, CHM-O-10.03]
prerequisites: [kc.chm.o.stoichiometry.percentage_composition]
related: [chm-o-stoichiometry, chm-o-metals]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [chem_molar_mass, math_evaluate]
visuals: [library_diagram]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: percentage
    unit: percent
    min: 1
    max: 85
    context: percentage by mass of nitrogen in fertiliser compounds and ammonia
  - quantity: molar_mass
    unit: g/mol
    min: 17
    max: 200
    context: relative formula masses of common nitrogen fertilisers
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student describes the tests for water and says what each stage of water treatment removes
  - The student reaches a checked percentage of nitrogen and uses it to compare fertilisers
  - The student matches a common air pollutant to its source, its effect and one way to reduce it
  - The student explains how greenhouse gases warm the Earth and names ways to cut their emissions
---

# Water, fertilisers, air and climate

## When to use

Use when a question asks how to test for water, or what each stage of treating the water supply does.
Use it for the percentage of nitrogen in a fertiliser, or for choosing the fertiliser that supplies more nitrogen.
Use it for the sources, effects and control of air pollutants, and for greenhouse gases and climate change.

Not for: percentage composition of compounds in general (chm-o-stoichiometry) or the rusting of iron (chm-o-metals).

## What this skill does not do

- It does not teach the Haber process conditions or equilibrium.
- It does not debate climate policy; it keeps to the chemistry.
- It does not give a pollutant's effect before the student has tried.

## Objectives

1. The student describes the tests for water and says what each stage of water treatment removes.
2. The student reaches a checked percentage of nitrogen and uses it to compare fertilisers.
3. The student matches a common air pollutant to its source, its effect and one way to reduce it.
4. The student explains how greenhouse gases warm the Earth and names ways to cut their emissions.

## Prerequisite probe

- "What is the percentage by mass of carbon in methane, CH₄? Use Ar C = 12 and H = 1." Expected: 75 % (checked with math_evaluate).

If it fails, detour to percentage composition in chm-o-stoichiometry, then return.

## Teaching sequence

1. Ask the student to name the substance in the question and what the question wants: a test, a stage, a source, an effect, a remedy or a percentage. Done when the target is named.
2. For water, ask for the test reagent, its colour before and after, and what a positive result does and does not prove. Done when the student separates "water is present" from "the water is pure".
3. For a fertiliser, ask for its formula and the number of nitrogen atoms in it; the chem_molar_mass result is the reference value for Mr. Done when the nitrogen count and Mr are on the board.
4. Ask for the mass of nitrogen over Mr, times 100; the math_evaluate result is the reference value. Done when the student's percentage matches the checked value to three significant figures.
5. For a pollutant, ask where it comes from, what harm it does and what removes or reduces it. Done when all three parts match the same pollutant.
6. For climate, ask which radiation the gases absorb and where that radiation comes from. Done when the student says the gases absorb infrared given off by the warm Earth.

## Hint ladder

1. Orient: "Which substance is this about, and what exactly does the question ask for?"
2. Concept: name the idea, such as "percentage of nitrogen is the mass of all the nitrogen atoms over Mr, times 100".
3. First step: give the formula or the source and ask for the next piece.
4. Worked step: show the nitrogen mass or the pollutant's source and ask for the effect or the result.
5. Full step: show the checked answer with its reason; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.environment_water_test_shows_purity`: thinks blue copper(II) sulfate proves pure water → "Sea water also turns anhydrous copper(II) sulfate blue; is sea water pure?"
- `mc.chm.o.environment_water_test_colours_reversed`: says copper(II) sulfate turns from blue to white → "Before the test the powder is anhydrous, with no water; what colour is it then?"
- `mc.chm.o.environment_treatment_stages_mixed`: thinks filtration kills microbes → "The filter traps grit and sand; which stage adds a chemical that kills bacteria?"
- `mc.chm.o.environment_n_atoms_count_ignored`: uses 14 for ammonium sulfate's nitrogen → "How many nitrogen atoms are in one (NH₄)₂SO₄?"
- `mc.chm.o.environment_n_percent_inverted`: divides Mr by the nitrogen mass → "Can a part of a compound be more than 100 % of its mass?"
- `mc.chm.o.environment_percent_not_times_100`: gives 0.35 as the percentage → "Is 0.35 a fraction or a percentage, and what turns one into the other?"
- `mc.chm.o.environment_pollutant_effects_mixed`: says carbon monoxide causes acid rain → "Acid rain needs a gas that dissolves to give an acid; does carbon monoxide do that, and what does it do to the blood?"
- `mc.chm.o.environment_nox_from_fuel_nitrogen`: thinks nitrogen oxides come from the fuel → "Petrol is mostly carbon and hydrogen; where else could the nitrogen come from in a hot engine?"
- `mc.chm.o.environment_converter_removes_co2`: thinks a catalytic converter removes carbon dioxide → "A converter turns carbon monoxide into which gas, and is that gas a greenhouse gas?"
- `mc.chm.o.environment_greenhouse_ozone_confused`: blames climate change on the ozone hole → "Is the warming caused by more sunlight getting in, or by less heat getting out?"
- `mc.chm.o.environment_greenhouse_blocks_sunlight`: thinks greenhouse gases block sunlight → "If the gases blocked sunlight, would the Earth get warmer or cooler?"
- `mc.chm.o.environment_climate_wrong_gas`: names sulfur dioxide as the main greenhouse gas → "Which two gases from burning fuels and farming trap the most heat?"

## Confidence and escalation

- Two mixed-up pollutant answers: switch to a table with columns for source, effect and remedy.
- Three failures on the nitrogen percentage: detour to percentage composition, then return.
- A percentage above 100: treat it as the inverted fraction, not a slip.
- A question about local water supply or air quality: save it for the teacher.

## Worked exchange

> Tutor: Ammonium nitrate, NH₄NO₃, has Mr 80. Ammonium sulfate, (NH₄)₂SO₄, has Mr 132. Use Ar N = 14. Which supplies the larger percentage of nitrogen? Start with ammonium sulfate.
> Student: 14 ÷ 132 × 100.
> Tutor: How many nitrogen atoms are in one (NH₄)₂SO₄?
> Student: Two. So 28 ÷ 132 × 100 = 21.2 %.
> Tutor: Right (checked with the maths tool). Now ammonium nitrate.
> Student: It also has two nitrogen atoms, so 28 ÷ 80 × 100 = 35.0 %. Ammonium nitrate supplies more.
> Tutor: Correct: 35.0 % against 21.2 % (checked with the maths tool).

## Verification checklist

- Every Mr shown has a check from chem_molar_mass, and every percentage a check from math_evaluate.
- The nitrogen mass counts every nitrogen atom in the formula.
- A percentage lies between 0 and 100 and has three significant figures unless the item says otherwise.
- Each pollutant's source, effect and remedy belong to the same gas.

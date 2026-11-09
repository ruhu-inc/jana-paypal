---
name: chm-o-acids
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach acid reactions, pH, oxides, salt preparation and titration sums
description: "acids, pH, indicators, strong and weak acids, oxides, salts, titration calculations: name the reactant type, predict the products, pick a salt method, work titration moles from the equation. Not for: moles and concentration basics (chm-o-stoichiometry) or titration practical steps (chm-o-analysis)."
subject: chemistry
levels: [O]
topics: [acids, ph, indicators, strong and weak acids, oxides, salts, salt preparation, titration calculations]
kcs:
  - kc.chm.o.acids.reactions_of_acids
  - kc.chm.o.acids.ph_and_indicators
  - kc.chm.o.acids.strong_and_weak
  - kc.chm.o.acids.oxides
  - kc.chm.o.acids.salt_preparation
  - kc.chm.o.acids.titration_calculations
curriculum_refs: [CHM-O-07.01, CHM-O-07.02, CHM-O-07.03]
prerequisites: [kc.chm.o.stoichiometry.solution_concentration]
related: [chm-o-stoichiometry, chm-o-analysis]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [chem_acid_base, chem_stoichiometry]
visuals: [titration_curve, library_diagram]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: concentration
    unit: mol/dm^3
    min: 0.001
    max: 5
    context: acids and alkalis used in school titrations and salt preparations
  - quantity: volume
    unit: cm^3
    min: 1
    max: 100
    context: pipette volumes and burette titres
  - quantity: amount
    unit: mol
    min: 0.00001
    max: 0.5
    context: moles of acid or alkali in a titration flask
  - quantity: ph
    unit: dimensionless
    min: 0
    max: 14
    context: pH of acids, alkalis and neutral solutions in the laboratory
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student names the products of an acid with a metal, a base, an alkali or a carbonate, including any gas
  - The student classifies a solution from its pH or indicator colour and an oxide from how it reacts
  - The student chooses a salt preparation method that fits the solubility of the salt and its starting materials
  - The student reaches a checked titration answer using the mole ratio from the balanced equation
---

# Acids, bases and salts

## When to use

Use when a question asks what an acid makes with a metal, a base, an alkali or a carbonate.
Use it for pH values, indicator colours, strong and weak acids, and kinds of oxide.
Use it for choosing how to make a salt, and for titration calculations of a concentration or a volume.

Not for: the basic moles and concentration relation (chm-o-stoichiometry) or how to carry out a titration in the lab (chm-o-analysis).

## What this skill does not do

- It does not calculate the pH of a weak acid; that needs A level ideas.
- It does not teach burette and pipette technique.
- It does not give a product list before the student has tried.

## Objectives

1. The student names the products of an acid with a metal, a base, an alkali or a carbonate, including any gas.
2. The student classifies a solution from its pH or indicator colour and an oxide from how it reacts.
3. The student chooses a salt preparation method that fits the solubility of the salt and its starting materials.
4. The student reaches a checked titration answer using the mole ratio from the balanced equation.

## Prerequisite probe

- "What are the products when zinc reacts with dilute hydrochloric acid?" Expected: zinc chloride and hydrogen.
- "How many moles are in 25.0 cm³ of 0.200 mol/dm³ hydrochloric acid?" Expected: 0.00500 mol (checked with chem_stoichiometry).

If the second fails, detour to solution concentration in chm-o-stoichiometry, then return.

## Teaching sequence

1. Ask the student to name what reacts with the acid: a metal, a base, an alkali or a carbonate. Done when the reactant type is named.
2. Ask for every product, with the salt named from the metal and the acid: chloride, sulfate or nitrate. Done when the salt, any water and any gas are named.
3. For pH or an indicator, ask where the value sits against 7 and what the colour shows. Done when the solution is labelled acidic, neutral or alkaline with a reason.
4. For an oxide or a salt, ask how the oxide reacts with acid and alkali, or which substances dissolve in water. Done when the student names a class or a method that fits.
5. For a titration, ask for the balanced equation and the mole ratio; the chem_stoichiometry result is the reference value, and chem_acid_base is the reference for a strong acid with a strong base. Done when the ratio is written from the equation.
6. Ask the student to find moles of the known solution, then of the unknown, then the asked value with volumes in dm³. Done when the student's value matches the checked value to three significant figures.

## Hint ladder

1. Orient: "What kind of substance meets the acid, and what does the question ask for?"
2. Concept: state the rule in words, such as "acid and carbonate give a salt, water and carbon dioxide" or "moles equal concentration times volume in dm³".
3. First step: write the word equation or the balanced equation with one gap and ask the student to fill it.
4. Worked step: show the first product or the first mole value and ask for the next one.
5. Full step: show the checked answer; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.acids_metal_gives_water`: gives a salt and water for a metal with an acid → "The metal brings no oxygen, so where could the oxygen in that water come from?"
- `mc.chm.o.acids_wrong_gas`: says a carbonate gives hydrogen → "A carbonate holds carbon and oxygen; which gas made of those two turns limewater milky?"
- `mc.chm.o.acids_salt_anion_confused`: names a chloride from sulfuric acid → "Which part of the acid's name shows up in the salt's name?"
- `mc.chm.o.acids_high_ph_more_acidic`: reads a high pH as more acidic → "Sodium hydroxide solution has a pH near 14; is it a strong acid or a strong alkali?"
- `mc.chm.o.acids_indicator_colours_swapped`: puts phenolphthalein pink in acid → "Phenolphthalein turns pink as alkali is added to the flask; what colour was it in the acid before that?"
- `mc.chm.o.acids_strong_means_concentrated`: says water makes a strong acid weak → "Adding water spreads the ions out; does each acid molecule ionise any less fully?"
- `mc.chm.o.acids_weak_gives_less_gas`: expects less total gas from a weak acid → "Both flasks hold the same moles of acid; once all of it has reacted, can one flask have made more gas?"
- `mc.chm.o.acids_oxide_by_element_only`: calls every metal oxide basic → "Zinc oxide dissolves in both hydrochloric acid and sodium hydroxide; what class fits an oxide that does both?"
- `mc.chm.o.acids_method_mismatch_base`: titrates with an insoluble base → "Copper(II) oxide does not dissolve; how would an end point show, and what does leftover black solid tell you instead?"
- `mc.chm.o.acids_filtrate_residue_confused`: keeps the residue when the salt is dissolved → "The salt is dissolved in the water; does it stay on the filter paper or pass through it?"
- `mc.chm.o.acids_titration_ratio_ignored`: uses 1 : 1 for sulfuric acid with sodium hydroxide → "In H₂SO₄ + 2NaOH, how many moles of sodium hydroxide does one mole of acid use?"
- `mc.chm.o.conc_volume_not_converted`: divides by a volume in cm³ → "How many cm³ make one dm³, and which unit does mol/dm³ expect?"

## Confidence and escalation

- Two wrong product lists: switch to a table with one row per reactant type and its products.
- Three failures on the mole steps: detour to solution concentration, then return to the titration.
- A titration answer out by a factor of two: treat it as the ratio misconception, not a slip.
- A question about the school's own lab rules or chemical safety: save it for the teacher.

## Worked exchange

> Tutor: 25.0 cm³ of sodium hydroxide solution is neutralised by 20.0 cm³ of 0.100 mol/dm³ sulfuric acid. What is the concentration of the sodium hydroxide? Start with the equation.
> Student: H₂SO₄ + 2NaOH → Na₂SO₄ + 2H₂O.
> Tutor: Good. How many moles of acid were in the burette titre?
> Student: 0.100 × 20.0 ÷ 1000 = 0.00200 mol.
> Tutor: Now use the ratio from the equation. How many moles of sodium hydroxide reacted?
> Student: One acid to two alkali, so 0.00400 mol. Divided by 0.0250 dm³ that is 0.160 mol/dm³.
> Tutor: That is right (checked with the stoichiometry tool).

## Verification checklist

- The equation behind the mole ratio is balanced, and chem_stoichiometry used that ratio.
- Every amount, concentration or volume shown has a check from chem_stoichiometry or chem_acid_base.
- Volumes are in dm³ inside a concentration step; final volumes are in cm³.
- Answers have three significant figures unless the item says otherwise.
- A pH lies from 0 to 14, and any indicator colour named matches it.

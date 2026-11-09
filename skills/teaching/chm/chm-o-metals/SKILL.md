---
name: chm-o-metals
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach metal properties, uses, alloys, reactivity, rusting and extraction
description: "metals, alloys, reactivity series, displacement, rusting, extraction, mass of metal from ore: use the reactivity series to predict, link a property to a use, and work extraction masses through moles. Not for: metallic bonding (chm-o-atoms) or electrolysis of molten ores (chm-o-electrolysis)."
subject: chemistry
levels: [O]
topics: [metals, alloys, reactivity series, displacement, rusting, corrosion, extraction of metals]
kcs:
  - kc.chm.o.metals.properties_of_metals
  - kc.chm.o.metals.properties_and_uses
  - kc.chm.o.metals.alloys
  - kc.chm.o.metals.reactivity_series
  - kc.chm.o.metals.corrosion
  - kc.chm.o.metals.extraction
  - kc.chm.o.metals.extraction_yield
curriculum_refs: [CHM-O-09.01, CHM-O-09.02, CHM-O-09.03, CHM-O-09.04, CHM-O-09.05, CHM-O-09.06]
prerequisites: [kc.chm.o.atoms.metallic_bonding, kc.chm.o.stoichiometry.reacting_masses]
related: [chm-o-atoms, chm-o-electrolysis, chm-o-stoichiometry]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [chem_stoichiometry]
visuals: [particle_diagram, library_diagram]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: mass
    unit: kg
    min: 0.0001
    max: 10000000
    context: from grams of oxide in the laboratory to thousands of tonnes of ore in industry
  - quantity: molar_mass
    unit: g/mol
    min: 20
    max: 300
    context: ores and oxides such as iron(III) oxide, aluminium oxide and zinc sulfide
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student links a metal's use to the property that matters and explains why an alloy is harder than the pure metal
  - The student predicts reactions with water, acids and metal salts from the reactivity series
  - The student states the conditions for rusting and picks an extraction method from a metal's reactivity
  - The student reaches a checked mass of metal from a mass of ore through moles and the equation's ratio
---

# Metals

## When to use

Use when a question links a metal's use to its properties, or asks why an alloy is harder than the pure metal.
Use it to predict whether a metal reacts with water, steam, an acid or another metal's salt.
Use it for rusting and its prevention, for choosing how to extract a metal, and for the mass of metal from an ore.

Not for: how metallic bonding works (chm-o-atoms) or the electrolysis of molten aluminium oxide (chm-o-electrolysis).

## What this skill does not do

- It does not rank metals that are not in the reactivity series the pack uses.
- It does not explain electrode equations for extraction by electrolysis.
- It does not give a prediction before the student has placed both metals in the series.

## Objectives

1. The student links a metal's use to the property that matters and explains why an alloy is harder than the pure metal.
2. The student predicts reactions with water, acids and metal salts from the reactivity series.
3. The student states the conditions for rusting and picks an extraction method from a metal's reactivity.
4. The student reaches a checked mass of metal from a mass of ore through moles and the equation's ratio.

## Prerequisite probe

- "What holds the particles together in a metal?" Expected: attraction between positive ions and a sea of delocalised electrons.
- "How many moles are in 48 g of magnesium? Use Ar Mg = 24." Expected: 2.00 mol (checked with chem_stoichiometry).

If the first fails, detour to metallic bonding (chm-o-atoms); if the second fails, to reacting masses (chm-o-stoichiometry).

## Teaching sequence

1. Ask the student to place each metal in the question on the reactivity series, with carbon and hydrogen marked. Done when the order is on the board.
2. For a reaction, ask whether the metal is above hydrogen or above the other metal, then name the products. Done when the prediction follows from the order.
3. For a use or an alloy, ask which property the job needs, and draw the layers of atoms. Done when the student links the property to the use or to layers that cannot slide.
4. For rusting or extraction, ask what iron needs to rust, or whether the metal sits above or below carbon. Done when the student names both conditions or the right method.
5. For a mass of metal, ask for the balanced equation, the moles of ore and the ratio; the chem_stoichiometry result is the reference value. Done when the moles of metal are written from the ratio.
6. Ask for the mass of metal from its moles and Ar, in the unit of the question. Done when the student's mass matches the checked value to three significant figures.

## Hint ladder

1. Orient: "Where do these metals sit in the reactivity series, and what does the question ask for?"
2. Concept: state the rule, such as "a more reactive metal pushes a less reactive one out of its compound".
3. First step: show the two metals on the series and ask which is higher.
4. Worked step: show the moles of ore or the predicted products and ask for the next step.
5. Full step: show the checked answer with its reason; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.metals_use_wrong_property`: says copper is used for wires because it is hard → "What must a wire do with electricity, and how well does copper do it?"
- `mc.chm.o.metals_conduct_by_moving_ions`: says metal ions carry the current → "In a solid metal the ions are fixed in layers; which particles are free to move?"
- `mc.chm.o.metals_alloy_stronger_bonds`: credits new strong bonds for an alloy's hardness → "When atoms of a different size sit among the layers, how easily can the layers slide?"
- `mc.chm.o.metals_displacement_reversed`: says copper displaces zinc from zinc sulfate → "Which of the two is higher in the series, so which one holds on to the sulfate?"
- `mc.chm.o.metals_all_metals_react`: expects copper to give hydrogen with dilute acid → "Is copper above or below hydrogen in the series?"
- `mc.chm.o.metals_gas_is_oxygen`: names oxygen as the gas from magnesium and acid → "A lighted splint pops in the gas; which gas does that show?"
- `mc.chm.o.metals_rust_one_condition`: thinks iron rusts in water alone → "A nail in boiled water under a layer of oil stays bright; what is missing there?"
- `mc.chm.o.metals_sacrificial_less_reactive`: picks copper for sacrificial protection → "To be eaten away instead of the iron, must the metal be more or less reactive than iron?"
- `mc.chm.o.metals_carbon_reduces_all`: extracts aluminium with carbon → "Is aluminium above or below carbon, so can carbon take the oxygen from it?"
- `mc.chm.o.metals_reduction_gains_oxygen`: says the ore is reduced by gaining oxygen → "Iron(III) oxide becomes iron; did it gain oxygen or lose it?"
- `mc.chm.o.metals_yield_mass_ratio`: multiplies the ore's mass by the equation's numbers → "Do the numbers in the equation count grams or particles?"
- `mc.chm.o.metals_yield_ratio_ignored`: takes one mole of Fe₂O₃ to give one mole of iron → "How many iron atoms are in one Fe₂O₃?"

## Confidence and escalation

- Two wrong predictions: switch to drawing the series as a ladder and placing both metals on it.
- Three failures on the mass of metal: detour to reacting masses, then return.
- A mass of metal larger than the mass of ore: treat it as a ratio or mass-ratio error, not a slip.
- A question about a local mine or factory process: save it for the teacher.

## Worked exchange

> Tutor: Iron is extracted by Fe₂O₃ + 3CO → 2Fe + 3CO₂. What mass of iron can be made from 320 g of iron(III) oxide? Use Ar Fe = 56 and O = 16.
> Student: The equation has 2 Fe, so 2 × 320 = 640 g.
> Tutor: Do the numbers in the equation count grams or particles? How many moles of Fe₂O₃ are in 320 g?
> Student: Particles. Mr is 2 × 56 + 3 × 16 = 160, so 320 ÷ 160 = 2.00 mol.
> Tutor: Good. How many moles of iron does that give?
> Student: Twice as many, 4.00 mol, so 4.00 × 56 = 224 g.
> Tutor: Correct (checked with the stoichiometry tool): 224 g of iron, less than the mass of ore, as it must be.

## Verification checklist

- Every Mr, amount and mass shown has a check from chem_stoichiometry.
- The equation behind the ratio is balanced, and the ratio is metal over ore.
- The mass of metal is less than the mass of ore, in the unit the question uses.
- Answers have three significant figures unless the item says otherwise.

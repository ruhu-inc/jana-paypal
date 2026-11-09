---
name: chm-o-stoichiometry
kind: teaching
version: "0.2"
status: draft
language: en
summary: Teach formulae, equations and mole calculations from masses to yields
description: "Formulae, balancing equations, moles, reacting masses, gas volumes, concentration: write the formula, balance with coefficients, convert to moles, use the equation ratio, convert back. Not for: titration results (chm-o-acids) or electrolysis masses (chm-o-electrolysis)."
subject: chemistry
levels: [O]
topics: [formulae, balancing equations, moles, reacting masses, gas volumes, concentration, empirical formula, percentage yield]
kcs:
  - kc.chm.o.stoichiometry.ionic_formulae
  - kc.chm.o.stoichiometry.balancing_equations
  - kc.chm.o.stoichiometry.relative_formula_mass
  - kc.chm.o.stoichiometry.percentage_composition
  - kc.chm.o.stoichiometry.moles_and_mass
  - kc.chm.o.stoichiometry.reacting_masses
  - kc.chm.o.stoichiometry.gas_volumes
  - kc.chm.o.stoichiometry.solution_concentration
  - kc.chm.o.stoichiometry.empirical_formula
  - kc.chm.o.stoichiometry.yield_and_purity
curriculum_refs: [CHM-O-03.01, CHM-O-03.02, CHM-O-03.03]
prerequisites:
  - kc.chm.o.atoms.ion_formation
related: [chm-o-acids, chm-o-electrolysis, chm-o-atoms]
pedagogy: [pedagogy-worked-example-fade]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [chem_molar_mass, chem_balance, chem_stoichiometry, math_evaluate]
visuals: [balanced_equation, knowns_panel, working_board]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: worked_example
plausibility:
  - quantity: molar_mass
    unit: g/mol
    min: 2
    max: 500
    context: elements and compounds in O level questions, including hydrated salts
  - quantity: mass
    unit: g
    min: 0.001
    max: 1000
    context: laboratory masses; a tonne-scale item may be flagged and needs its unit checked
  - quantity: amount
    unit: mol
    min: 0.00001
    max: 100
    context: amounts of substance in laboratory and simple industrial questions
  - quantity: volume
    unit: dm^3
    min: 0.001
    max: 500
    context: gas volumes at room conditions and solution volumes
  - quantity: concentration
    unit: mol/dm^3
    min: 0.001
    max: 20
    context: solutions used in school preparations and titrations
  - quantity: percentage_yield
    unit: percent
    min: 0
    max: 100
    context: actual mass of product as a share of the calculated mass
  - quantity: percentage_purity
    unit: percent
    min: 0
    max: 100
    context: pure substance as a share of an impure sample
  - quantity: percentage_composition
    unit: percent
    min: 0
    max: 100
    context: mass of one element as a share of a compound
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student writes correct formulae and balances an equation by changing coefficients only
  - The student converts between mass, moles, gas volume and concentration with the right relation and units
  - The student uses the mole ratio from the balanced equation to reach a checked mass, volume, formula or percentage
---

# Formulae, equations and moles

## When to use

Use when a question asks for an ionic formula, a balanced equation, a relative formula mass, a percentage by mass,
an amount in moles, a reacting mass, a gas volume at room conditions, a concentration, an empirical formula, or a
percentage yield or purity.

Not for: concentrations from titration results (chm-o-acids), or masses made by electrolysis (chm-o-electrolysis).

## What this skill does not do

- It does not use 22.4 dm³ per mole; room conditions use the pack's molar gas volume of 24 dm³.
- It does not treat limiting reagents beyond saying which reactant is in excess when the question names it.
- It does not choose relative atomic masses: the question or the pack's values give them.

## Calculation focus

For a mass-to-amount calculation, amount is mass divided by molar mass; preserve their order and units.
Use a balanced-equation mole ratio only when that reaction is part of the question. Gas and solution calculations
must use the stated units and the applicable molar-volume convention. Yield, purity and composition use the
appropriate part divided by the stated whole. Propose arithmetic using source quantities; the worker executes
it. Do not demonstrate a different problem, invent a reaction or ask for givens already supplied.

## Coaching focus

When Given and Find are ready, ask about the relation, not for the givens again.
Molar mass is mass per mole, not the amount of substance. Amount is mass divided by molar mass; preserve that
order and the source units. For a simple mass-to-amount question, do not invent an equation ratio or a universal
arithmetic rule. After a checked final answer, confirm completion and invite a new question.

## Objectives

1. The student writes correct formulae and balances an equation by changing coefficients only.
2. The student converts between mass, moles, gas volume and concentration with the right relation and units.
3. The student uses the mole ratio from the balanced equation to reach a checked mass, volume, formula or percentage.

## Prerequisite probe

- "What charge is on an aluminium ion, and on an oxide ion?" Expected: Al³⁺ and O²⁻.

If it fails, detour to ion formation (chm-o-atoms), then return.

## Teaching sequence

1. Ask the student to write each formula with its ion charges balanced; the chem_molar_mass result confirms the formula and gives its molar mass. Done when every formula is accepted by the tool.
2. Ask the student to balance the equation with coefficients only; the chem_balance result is the reference. Done when the student's coefficients match the checked equation in lowest whole numbers.
3. Ask the student to list the givens with units and mark the unknown. Done when every given is on the knowns panel with its unit.
4. Ask the student to turn the given into moles: mass ÷ molar mass, gas volume ÷ 24 dm³, or concentration × volume in dm³. Done when the student's amount matches the chem_stoichiometry result.
5. Ask for the mole ratio from the balanced equation, then the unknown amount and its conversion back; the chem_stoichiometry result is the reference value. Done when the student's final value matches the checked value with its unit.
6. For a percentage, empirical formula, yield or purity, ask the student to set up the fraction in words first; math_evaluate gives the reference value. Done when the fraction has the part on top and the checked value is stated.

## Hint ladder

1. Orient: "What does the question give you, and what does it ask for?"
2. Concept: "Equations count particles, so every route goes through moles."
3. First step: write amount = mass ÷ molar mass and ask the student to fill it in.
4. Worked step: show the amount and the mole ratio and ask the student for the unknown amount.
5. Full step: show the checked answer with every line; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.formula_own_charge_as_count`: uses an ion's own charge as its number → "If you take two Al³⁺ and three O²⁻, do the charges cancel?"
- `mc.chm.o.subscript_changed_to_balance`: changes H₂O to H₂O₂ to balance → "Is H₂O₂ still water?"
- `mc.chm.o.mr_bracket_multiplier`: counts Ca(OH)₂ as Ca + O + H₂ → "How many oxygen atoms does Ca(OH)₂ contain?"
- `mc.chm.o.percent_count_ignored`: uses one atom on top for a formula with several → "How many nitrogen atoms does NH₄NO₃ contain, and so how much nitrogen mass goes on top?"
- `mc.chm.o.moles_ratio_inverted`: divides molar mass by mass → "If 100 g of a substance with molar mass 100 g/mol is one mole, how many moles is 50 g?"
- `mc.chm.o.mass_ratio_as_mole_ratio`: scales grams by the equation numbers → "Do the numbers in front of the formulae count grams or particles?"
- `mc.chm.o.ratio_ignored`: assumes moles of product equal moles of reactant → "What do the numbers in front of the two formulae say about how many of each?"
- `mc.chm.o.gas_stp_volume`: uses 22.4 dm³ at room conditions → "At what temperature is one mole of gas 22.4 dm³, and is the room that cold?"
- `mc.chm.o.conc_volume_not_converted`: divides by cm³ for mol/dm³ → "How many cm³ are in one dm³?"
- `mc.chm.o.empirical_rounded_early`: rounds 1 : 1.5 to 1 : 2 → "What whole number can you multiply 1.5 by to get a whole number?"
- `mc.chm.o.yield_uses_reactant_mass`: uses the reactant mass as the theoretical yield → "Is the theoretical yield a mass of reactant or of product?"

## Confidence and escalation

- Two failures on the mole ratio: switch to a three-row table of moles under each formula.
- Three failures on converting to moles: give a worked example, then a similar item with one step faded.
- A formula outside the pack, such as an unusual complex ion: save it as a local question for human help.

## Worked exchange

> Tutor: CaCO₃ → CaO + CO₂. What mass of calcium oxide forms when 25.0 g of calcium carbonate is heated? Use Ca = 40, C = 12, O = 16.
> Student: The molar mass of CaCO₃ is 40 + 12 + 48 = 100 g/mol, so I have 25.0 ÷ 100 = 0.250 mol.
> Tutor: Good. What does the equation say about moles of CaO?
> Student: One CaCO₃ gives one CaO, so 0.250 mol of CaO.
> Tutor: And the mass of that much CaO?
> Student: 40 + 16 = 56 g/mol, so 0.250 × 56 = 14.0 g.
> Tutor: Correct (checked with the stoichiometry tool).

## Verification checklist

- Every formula passed chem_molar_mass and the equation passed chem_balance.
- Every amount, mass, volume and concentration matches a chem_stoichiometry or math_evaluate result.
- Volumes in cm³ were divided by 1000 before use with mol/dm³ or the molar gas volume.
- Percentages lie between 0 and 100.
- Answers are given to three significant figures unless the question says otherwise.

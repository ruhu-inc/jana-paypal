---
name: chm-o-electrolysis
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach electrolysis products, half-equations, uses and masses from charge
description: "Electrolysis, electrodes, half-equations, electroplating, Faraday calculations: list the ions, send each to the opposite electrode, choose the product, balance electrons, then go from charge to moles to mass. Not for: extraction by carbon (chm-o-metals) or general moles (chm-o-stoichiometry)."
subject: chemistry
levels: [O]
topics: [electrolysis, electrodes, half-equations, electroplating, faraday calculations]
kcs:
  - kc.chm.o.electrolysis.molten_products
  - kc.chm.o.electrolysis.aqueous_products
  - kc.chm.o.electrolysis.half_equations
  - kc.chm.o.electrolysis.applications
  - kc.chm.o.electrolysis.quantitative
curriculum_refs: [CHM-O-04.01]
prerequisites:
  - kc.chm.o.atoms.ion_formation
  - kc.chm.o.stoichiometry.balancing_equations
  - kc.chm.o.stoichiometry.moles_and_mass
related: [chm-o-metals, chm-o-stoichiometry, chm-o-atoms]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [chem_balance, chem_stoichiometry, math_evaluate]
visuals: [library_diagram, balanced_equation, knowns_panel]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: current
    unit: A
    min: 0.01
    max: 20
    context: currents through school electrolysis cells
  - quantity: time
    unit: s
    min: 1
    max: 100000
    context: electrolysis runs from seconds to about a day
  - quantity: charge
    unit: C
    min: 0.1
    max: 500000
    context: charge passed in a school cell, current multiplied by time in seconds
  - quantity: amount
    unit: mol
    min: 0.000001
    max: 5
    context: moles of electrons or of product in a school cell
  - quantity: mass
    unit: g
    min: 0.0001
    max: 100
    context: mass of metal deposited on a cathode
  - quantity: volume
    unit: dm^3
    min: 0.0001
    max: 50
    context: gas collected at an electrode at room conditions
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student names the product at each electrode for a molten or aqueous electrolyte with a reason
  - The student writes balanced half-equations with electrons on the correct side
  - The student describes the electrodes and electrolyte for electroplating, copper purification and aluminium extraction
  - The student calculates a mass of product from current and time through moles of electrons
---

# Electrolysis

## When to use

Use when a question asks what forms at the anode or cathode, for a half-equation, how an object is electroplated or
copper is purified, why aluminium is made by electrolysis, or what mass forms for a given current and time.

Not for: extracting metals by heating with carbon (chm-o-metals), or mole calculations with no current (chm-o-stoichiometry).

## What this skill does not do

- It does not use electrode potentials; it uses the reactivity series and the concentration rule.
- It does not treat cells that make electricity.
- It does not set the Faraday constant: the pack gives 96,500 C per mole of electrons.

## Objectives

1. The student names the product at each electrode for a molten or aqueous electrolyte with a reason.
2. The student writes balanced half-equations with electrons on the correct side.
3. The student describes the electrodes and electrolyte for electroplating, copper purification and aluminium extraction.
4. The student calculates a mass of product from current and time through moles of electrons.

## Prerequisite probe

- "What ions are in molten lead(II) bromide?" Expected: Pb²⁺ and Br⁻.
- "How many moles are in 6.4 g of copper? Cu = 64." Expected: 0.10 mol.

If the first fails, detour to ion formation (chm-o-atoms); if the second fails, detour to moles (chm-o-stoichiometry).

## Teaching sequence

1. Ask the student to list every ion present, including H⁺ and OH⁻ from water when the electrolyte is aqueous. Done when the list is complete and says molten or aqueous.
2. Ask which ions go to the cathode (positive ions) and which to the anode (negative ions). Done when each ion is sent to the opposite charge.
3. Ask which ion is discharged: in a melt, the only ions present; in solution, hydrogen unless the metal is below hydrogen, and a halogen from a concentrated halide, otherwise oxygen. Done when one product per electrode is named with its rule.
4. Ask for each half-equation with electrons gained at the cathode and lost at the anode; the chem_balance result is the reference. Done when atoms and charges balance as checked.
5. For a mass, ask for charge = current × time in seconds, then moles of electrons = charge ÷ 96,500, then moles of product from the half-equation; the chem_stoichiometry result is the reference value. Done when the student's mass matches the checked value.
6. For an application, ask which electrode the object or impure metal is and what the electrolyte contains. Done when both electrodes and the electrolyte are named correctly.

## Hint ladder

1. Orient: "Is the electrolyte molten or dissolved in water?"
2. Concept: "Positive ions go to the cathode and gain electrons; negative ions go to the anode and lose them."
3. First step: write the ions under the electrode they move to and ask which one is discharged.
4. Worked step: show the cathode half-equation and ask for the anode one.
5. Full step: show both products and half-equations, or the checked mass; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.electrolysis_metal_at_anode`: puts the metal at the anode → "A lead ion is Pb²⁺. Which electrode attracts a positive ion?"
- `mc.chm.o.electrolysis_molten_gives_hydrogen`: predicts hydrogen from a melt → "Is there any water in molten lead(II) bromide to supply hydrogen?"
- `mc.chm.o.electrolysis_reactive_metal_from_solution`: predicts sodium from brine → "If sodium formed in water, what would it do straight away?"
- `mc.chm.o.electrolysis_halide_concentration_ignored`: gives chlorine from very dilute chloride solution → "When chloride ions are scarce, which other negative ion is waiting at the anode?"
- `mc.chm.o.electrolysis_electrons_wrong_side`: writes electrons as products at the cathode → "Does a positive ion become an atom by gaining or by losing electrons?"
- `mc.chm.o.electrolysis_single_atoms_formed`: writes Cl as the anode product → "How do chlorine atoms exist in the gas you can smell?"
- `mc.chm.o.electrolysis_impure_copper_as_cathode`: makes impure copper the cathode → "Which electrode gets thinner as its copper goes into solution?"
- `mc.chm.o.electrolysis_plated_object_as_anode`: makes the spoon the anode → "Metal ions gain electrons to coat an object. At which electrode do ions gain electrons?"
- `mc.chm.o.electrolysis_time_not_in_seconds`: uses minutes in Q = I × t → "One coulomb is one ampere for how long?"
- `mc.chm.o.electrolysis_electron_ratio_ignored`: takes moles of copper as moles of electrons → "How many electrons does each Cu²⁺ ion need?"

## Confidence and escalation

- Two wrong electrode choices: switch to a cell diagram with the ions drawn moving.
- Three failures on the mass route: detour to moles and mass, then return.
- A mixture of ions or an electrode that reacts in a way outside the pack: save it as a local question for human help.

## Worked exchange

> Tutor: A current of 1.00 A passes through copper(II) sulfate solution for 32 minutes 10 seconds. What mass of copper forms at the cathode? Cu = 64, and one mole of electrons carries 96,500 C.
> Student: The time is 32 × 60 + 10 = 1930 s, so the charge is 1930 C.
> Tutor: Right. How many moles of electrons is that?
> Student: 1930 ÷ 96,500 = 0.0200 mol.
> Tutor: Good. Write the cathode half-equation. How many moles of copper does that charge give?
> Student: Cu²⁺ + 2e⁻ → Cu, so half as many: 0.0100 mol, and 0.0100 × 64 = 0.640 g.
> Tutor: Correct (checked with the stoichiometry tool).

## Verification checklist

- Every half-equation passed chem_balance for atoms and charge.
- The time was in seconds before Q = I × t, checked with math_evaluate.
- Moles of product = moles of electrons ÷ electrons per ion, matching chem_stoichiometry.
- Halogen and oxygen products are written as diatomic molecules.
- Answers are given to three significant figures unless the question says otherwise.

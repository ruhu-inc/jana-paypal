---
name: chm-o-energetics
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach exothermic and endothermic changes, energy profiles and enthalpy sums
description: "Exothermic, endothermic, enthalpy change, energy profiles, bond energies, calorimetry: decide the direction of energy flow, read the profile from the reactants, total bonds broken minus formed, and divide Q by moles. Not for: rates of reaction (chm-o-reactions)."
subject: chemistry
levels: [O]
topics: [exothermic, endothermic, enthalpy change, energy profiles, bond energies, calorimetry]
kcs:
  - kc.chm.o.energetics.exo_endo
  - kc.chm.o.energetics.profiles
  - kc.chm.o.energetics.bond_energies
  - kc.chm.o.energetics.calorimetry
curriculum_refs: [CHM-O-05.01]
prerequisites:
  - kc.chm.o.stoichiometry.moles_and_mass
related: [chm-o-reactions, chm-o-stoichiometry]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [chem_energetics, math_evaluate]
visuals: [energy_profile, knowns_panel]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: enthalpy_change
    unit: kJ/mol
    min: -6000
    max: 1000
    context: combustion, neutralisation, dissolving and decomposition at O level
  - quantity: activation_energy
    unit: kJ/mol
    min: 1
    max: 500
    context: barriers read from reaction pathway diagrams
  - quantity: energy
    unit: kJ
    min: 0.01
    max: 500
    context: energy transferred to water in a school calorimetry experiment
  - quantity: temperature
    unit: degC
    min: -40
    max: 100
    context: thermometer readings and temperature changes in water or dilute solution
  - quantity: mass
    unit: g
    min: 0.01
    max: 1000
    context: mass of water heated or of fuel burned
  - quantity: amount
    unit: mol
    min: 0.0001
    max: 10
    context: moles of fuel or reactant used up in the experiment
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student classifies a change as exothermic or endothermic from a temperature change or the sign of the enthalpy change
  - The student reads the enthalpy change and activation energy from a profile, both measured from the reactants
  - The student calculates an enthalpy change from bond energies or calorimetry data with the correct sign and unit
---

# Energy changes in reactions

## When to use

Use when a question asks whether a change is exothermic or endothermic, asks for ΔH or the activation energy from a
reaction pathway diagram, gives bond energies, or gives a temperature change for a mass of water heated by a reaction
or a burning fuel.

Not for: how temperature or a catalyst changes the rate (chm-o-reactions).

## What this skill does not do

- It does not use Hess cycles or standard enthalpies of formation.
- It does not correct calorimetry results for heat lost to the surroundings, beyond naming it.
- It does not set the specific heat capacity: the pack gives 4.2 J/g °C, and an item may state another.

## Objectives

1. The student classifies a change as exothermic or endothermic from a temperature change or the sign of the enthalpy change.
2. The student reads the enthalpy change and activation energy from a profile, both measured from the reactants.
3. The student calculates an enthalpy change from bond energies or calorimetry data with the correct sign and unit.

## Prerequisite probe

- "How many moles are in 4.6 g of ethanol? Its molar mass is 46 g/mol." Expected: 0.10 mol.

If it fails, detour to moles and mass (chm-o-stoichiometry), then return.

## Teaching sequence

1. Ask whether the surroundings got warmer or cooler. Done when the student says warmer means exothermic with negative ΔH, and cooler means endothermic with positive ΔH.
2. For a profile, ask the student to find the reactants' level first, then measure to the products for ΔH and to the peak for the activation energy; math_evaluate gives the reference values. Done when both values match and ΔH carries its sign.
3. For bond energies, ask for the total energy of bonds broken and of bonds formed, counting each bond as often as it appears; the chem_energetics result is the reference value. Done when ΔH = broken − formed matches the checked value.
4. For calorimetry, ask for Q = m × c × ΔT using the mass of water; the chem_energetics result is the reference value. Done when Q matches with its unit.
5. Ask the student to convert Q to kJ and divide by the moles that reacted. Done when the student's value matches the chem_energetics result in kJ/mol.
6. Ask for the sign from step 1 and a sense check against typical values. Done when the final ΔH has a sign, a unit and three significant figures.

## Hint ladder

1. Orient: "Did the surroundings get hotter or colder?"
2. Concept: "Breaking bonds takes energy in; making bonds gives energy out."
3. First step: write the relation, such as Q = m × c × ΔT, and ask the student to fill it in.
4. Worked step: show Q and the moles and ask the student for ΔH with its sign.
5. Full step: show the checked ΔH with every line; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.energetics_temp_rise_endo`: calls a temperature rise endothermic → "If the beaker feels hot, did the reaction give energy out or take it in?"
- `mc.chm.o.energetics_breaking_releases`: thinks breaking bonds releases energy → "Do you have to put energy in to pull two magnets apart?"
- `mc.chm.o.energetics_ea_from_zero`: measures the activation energy from zero → "Where does the reaction start on the diagram: at zero or at the reactants' line?"
- `mc.chm.o.energetics_catalyst_changes_dh`: thinks a catalyst changes ΔH → "With a catalyst, do the reactants or the products move up or down?"
- `mc.chm.o.energetics_formed_minus_broken`: works out formed − broken → "If more energy is given out than taken in, should ΔH be positive or negative?"
- `mc.chm.o.energetics_bond_count_ignored`: uses each bond energy once → "How many O–H bonds are in two molecules of water?"
- `mc.chm.o.energetics_mass_of_reactant`: puts the fuel mass in Q = mcΔT → "Which substance's temperature did the thermometer measure?"
- `mc.chm.o.energetics_q_not_per_mole`: gives Q as ΔH → "ΔH is per mole. How many moles of fuel burned?"
- `mc.chm.o.energetics_calorimetry_sign`: gives a positive ΔH for warming → "The water got hotter. Did the reacting chemicals gain energy or lose it?"

## Confidence and escalation

- Two sign errors: switch to an energy profile sketch and ask whether the products sit above or below the reactants.
- Three failures on dividing by moles: detour to moles and mass, then return.
- Data that mixes heat capacities of the container and the water: save it as a local question for human help.

## Worked exchange

> Tutor: Burning 0.46 g of ethanol (molar mass 46 g/mol) heats 200 g of water from 20.0 °C to 30.0 °C. Take c = 4.2 J/g °C. What is ΔH of combustion?
> Student: Q = 200 × 4.2 × 10.0 = 8400 J, which is 8.40 kJ.
> Tutor: Good. How many moles of ethanol burned?
> Student: 0.46 ÷ 46 = 0.0100 mol, so 8.40 ÷ 0.0100 = 840 kJ/mol.
> Tutor: The water got hotter. What sign does ΔH need?
> Student: Negative, so ΔH is −840 kJ/mol.
> Tutor: Correct (checked with the energetics tool). It is less negative than a data-book value because heat escapes.

## Verification checklist

- Q and ΔH match chem_energetics; any profile reading matches math_evaluate.
- The mass in Q = m × c × ΔT is the mass of water or solution, not of the reactant.
- Q was converted from J to kJ before dividing by moles.
- ΔH is negative when the surroundings warmed and positive when they cooled.
- Answers are given to three significant figures unless the question says otherwise.

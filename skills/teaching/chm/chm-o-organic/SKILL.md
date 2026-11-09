---
name: chm-o-organic
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach organic series, naming, fuels, reactions, polymers and combustion
description: "organic chemistry, homologous series, naming, alkanes, alkenes, alcohols, polymers, crude oil, combustion: find the functional group first, then name, predict or balance from it. Not for: balancing equations in general (chm-o-stoichiometry) or energy changes of fuels (chm-o-energetics)."
subject: chemistry
levels: [O]
topics: [organic, homologous series, naming, general formula, crude oil, cracking, alkanes, alkenes, alcohols, esters, polymers, combustion]
kcs:
  - kc.chm.o.organic.functional_groups
  - kc.chm.o.organic.naming
  - kc.chm.o.organic.general_formulae
  - kc.chm.o.organic.fuels_and_cracking
  - kc.chm.o.organic.alkanes_and_alkenes
  - kc.chm.o.organic.alcohols_and_acids
  - kc.chm.o.organic.carboxylic_acids
  - kc.chm.o.organic.polymers
  - kc.chm.o.organic.combustion
curriculum_refs: [CHM-O-11.01, CHM-O-11.02, CHM-O-11.03, CHM-O-11.04, CHM-O-11.05, CHM-O-11.06, CHM-O-11.07, CHM-O-11.08]
prerequisites: [kc.chm.o.stoichiometry.relative_formula_mass, kc.chm.o.stoichiometry.balancing_equations]
related: [chm-o-stoichiometry, chm-o-energetics]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [chem_molar_mass, chem_balance, chem_stoichiometry]
visuals: [balanced_equation]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: molar_mass
    unit: g/mol
    min: 16
    max: 300
    context: relative molecular masses of alkanes, alkenes, alcohols, acids and esters up to twenty carbons
  - quantity: mass
    unit: g
    min: 0.01
    max: 5000
    context: masses of fuel burned or carbon dioxide made in combustion questions
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student identifies the homologous series from the functional group and names the compound from its carbon count
  - The student uses a general formula to reach a checked molecular formula and relative molecular mass
  - The student describes fractional distillation, cracking, the bromine water test, making ethanol, esters and polymers
  - The student writes a checked balanced equation for complete combustion with the smallest whole numbers
---

# Organic chemistry

## When to use

Use when a question asks which homologous series a compound belongs to, or asks for its name or formula.
Use it for crude oil fractions and cracking, for telling alkanes from alkenes, and for ethanol, ethanoic acid and esters.
Use it for addition and condensation polymers, and for balancing the complete combustion of a hydrocarbon or an alcohol.

Not for: balancing equations outside organic chemistry (chm-o-stoichiometry) or energy given out by fuels (chm-o-energetics).

## What this skill does not do

- It does not name branched chains or compounds with more than four carbon atoms.
- It does not teach reaction mechanisms or isomer counting.
- It does not hand over a name or an equation before the student has tried.

## Objectives

1. The student identifies the homologous series from the functional group and names the compound from its carbon count.
2. The student uses a general formula to reach a checked molecular formula and relative molecular mass.
3. The student describes fractional distillation, cracking, the bromine water test, making ethanol, esters and polymers.
4. The student writes a checked balanced equation for complete combustion with the smallest whole numbers.

## Prerequisite probe

- "What is the relative molecular mass of CO₂? Use Ar C = 12 and O = 16." Expected: 44 (checked with chem_molar_mass).
- "Balance H₂ + O₂ → H₂O." Expected: 2H₂ + O₂ → 2H₂O.

If either fails, detour to relative formula mass or balancing equations in chm-o-stoichiometry, then return.

## Teaching sequence

1. Ask the student to find the functional group: C=C, –OH, –COOH or the ester link, or none. Done when the series is named from the group.
2. For a name, ask for the total number of carbon atoms, including the one in –COOH, then the prefix and the ending. Done when prefix and ending both fit.
3. For a general formula, ask the student to put n into it, or to solve for n from Mr; the chem_molar_mass result is the reference value. Done when the formula and Mr match the checked values.
4. For fuels and reactions, ask what changes: boiling point with molecule size, bonds broken in cracking, or the colour of bromine water. Done when the student names the change and its reason.
5. For ethanol, esters and polymers, ask for the reactants, conditions and every product, including any water. Done when each product and condition is named.
6. For combustion, ask the student to balance carbon, then hydrogen, then oxygen; the chem_balance result is the reference value. Done when the coefficients match the checked equation in lowest whole numbers.

## Hint ladder

1. Orient: "Which functional group does the compound have, and what does the question ask for?"
2. Concept: state the rule, such as "an alkene's double bond opens so bromine adds across it".
3. First step: write the series' general formula or the unbalanced equation and ask the student to fill one part.
4. Worked step: show the carbon and hydrogen balanced, or the formula for n, and ask for the last part.
5. Full step: show the checked answer with its reason; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.organic_oh_group_is_hydroxide`: expects ethanol to be alkaline → "Ethanol's –OH is joined to carbon by a shared pair; can it break off as an OH⁻ ion like in sodium hydroxide?"
- `mc.chm.o.organic_prefix_wrong_atoms`: names CH₃COOH as methanoic acid → "Count every carbon, including the one in –COOH; how many are there?"
- `mc.chm.o.organic_gf_hydrogen_mass_ignored`: solves 12n = 58 for an alkane → "What does each CH₂ unit weigh, and what do the two extra hydrogens add?"
- `mc.chm.o.organic_big_molecules_boil_low`: puts large molecules at the top of the column → "Which molecules have stronger forces between them, so which need the higher temperature to boil?"
- `mc.chm.o.organic_cracking_is_separation`: thinks cracking separates molecules already there → "Before cracking, was there any ethene in the heavy fraction?"
- `mc.chm.o.organic_alkanes_decolourise_bromine`: says ethane decolourises bromine water → "Which bond lets bromine add on, and does ethane have it?"
- `mc.chm.o.organic_ester_name_order`: names methanol and ethanoic acid's ester ethyl methanoate → "Which part of the name comes from the alcohol, the -yl part or the -oate part?"
- `mc.chm.o.organic_fermentation_needs_air_or_heat`: heats fermentation to 80 °C → "Yeast's enzymes are proteins; what happens to them well above body temperature?"
- `mc.chm.o.organic_polymer_keeps_double_bond`: draws C=C in poly(ethene) → "Each double bond opened to link to the next monomer; is any double bond left?"
- `mc.chm.o.organic_addition_condensation_mixed`: gives water as a product of addition polymerisation → "Every atom of every ethene molecule ends up in poly(ethene); is anything left over to make water?"
- `mc.chm.o.organic_incomplete_products_in_complete`: writes carbon monoxide for complete combustion → "With plenty of oxygen, what does all the carbon end up as?"
- `mc.chm.o.organic_oxygen_formula_changed`: writes oxygen as O to balance → "Oxygen gas is made of molecules; how many atoms are in each one?"

## Confidence and escalation

- Two wrong names: switch to a table of prefixes against carbon counts and endings against series.
- Three failures on balancing: detour to balancing equations, then return to combustion.
- A balanced equation that shares a common factor: ask the student to divide through before marking it.
- A question about the school's own fuel or plastic use: save it for the teacher.

## Worked exchange

> Tutor: Write a balanced equation for the complete combustion of ethane, C₂H₆. What are the products?
> Student: Carbon dioxide and water.
> Tutor: Good. Balance the carbon first, then the hydrogen.
> Student: C₂H₆ + O₂ → 2CO₂ + 3H₂O.
> Tutor: Now count the oxygen atoms on the right.
> Student: Seven, so three and a half O₂. Doubling gives 2C₂H₆ + 7O₂ → 4CO₂ + 6H₂O.
> Tutor: Correct (checked with the balancing tool): whole numbers with no common factor.

## Verification checklist

- Every Mr shown has a check from chem_molar_mass; every combustion mass has a check from chem_stoichiometry.
- Every equation shown passed chem_balance, with oxygen as O₂ and no formula changed.
- Coefficients are the smallest whole numbers.
- A name uses the total carbon count and the ending of the series the functional group shows.

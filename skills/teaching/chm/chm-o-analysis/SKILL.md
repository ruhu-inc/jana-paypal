---
name: chm-o-analysis
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach lab apparatus, titration technique, separation, purity and chemical tests
description: "chromatography, Rf values, separation, purity, apparatus, titration technique, tests for ions and gases: match the method or test to the property, read the result, give the reason. Not for: titration calculations (chm-o-acids) or states of matter (chm-o-states)."
subject: chemistry
levels: [O]
topics: [analysis, apparatus, titration technique, chromatography, rf values, separation, purity, ion tests, gas tests]
kcs:
  - kc.chm.o.analysis.apparatus
  - kc.chm.o.analysis.titration_technique
  - kc.chm.o.analysis.chromatography
  - kc.chm.o.analysis.separation
  - kc.chm.o.analysis.purity
  - kc.chm.o.analysis.cation_tests
  - kc.chm.o.analysis.anion_tests
  - kc.chm.o.analysis.gas_tests
curriculum_refs: [CHM-O-12.01, CHM-O-12.02, CHM-O-12.03, CHM-O-12.04, CHM-O-12.05]
prerequisites: [kc.chm.o.atoms.element_compound_mixture, kc.chm.o.states.changes_of_state]
related: [chm-o-acids, chm-o-atoms, chm-o-states]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [math_evaluate]
visuals: [library_diagram]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: rf_value
    unit: dimensionless
    min: 0
    max: 1
    context: spots on a paper chromatogram
  - quantity: distance
    unit: cm
    min: 0.1
    max: 30
    context: distances moved by spots and solvent fronts on chromatography paper
  - quantity: volume
    unit: cm^3
    min: 0.1
    max: 50
    context: burette readings and mean titres
  - quantity: temperature
    unit: degC
    min: -120
    max: 400
    context: melting and boiling points measured in a school laboratory
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student chooses apparatus and titration steps that give the accuracy the task needs
  - The student calculates an Rf value from the start line and chooses a separation method from the components' properties
  - The student judges purity from a melting or boiling point
  - The student identifies an ion or a gas from a named test and its observed result
---

# Analysis, separation and tests

## When to use

Use when a question asks which apparatus to use, or what order the steps of a titration go in.
Use it for Rf values and chromatograms, for separating a mixture, and for judging purity from a melting or boiling point.
Use it for identifying a cation, an anion or a gas from a test result.

Not for: titration calculations (chm-o-acids) or the particle model of melting and boiling (chm-o-states).

## What this skill does not do

- It does not run titration calculations of concentration.
- It does not use flame tests or instrumental methods.
- It does not name an ion before the student has given a reading of the result.

## Objectives

1. The student chooses apparatus and titration steps that give the accuracy the task needs.
2. The student calculates an Rf value from the start line and chooses a separation method from the components' properties.
3. The student judges purity from a melting or boiling point.
4. The student identifies an ion or a gas from a named test and its observed result.

## Prerequisite probe

- "Is sea water an element, a compound or a mixture?" Expected: a mixture.
- "What is the change from solid to liquid called, and does a pure solid's temperature rise while it happens?" Expected: melting; the temperature stays constant.

If either fails, detour to elements, compounds and mixtures (chm-o-atoms) or changes of state (chm-o-states), then return.

## Teaching sequence

1. Ask what the task needs: a quantity to measure, a substance to separate, a purity check or an unknown to identify. Done when the student names the task type.
2. For apparatus or titration steps, ask how exact the value must be and pick the item or order. Done when the choice fits the accuracy, such as a pipette for one exact volume.
3. For a chromatogram, ask for both distances from the start line, then the spot over the solvent front; the math_evaluate result is the reference value. Done when the student's Rf matches the checked value.
4. For a mixture, ask which property differs between the parts: solubility, boiling point or size of particle. Done when the method named uses that property.
5. For purity, ask whether the substance melts or boils sharply at the value in a data table. Done when the student says pure or impure with the reading as reason.
6. For an unknown ion or gas, ask for the reagent, the first observation and what happens in excess. Done when the identity follows from every observation given.

## Hint ladder

1. Orient: "What is the question trying to find out, and what was observed?"
2. Concept: name the idea, such as "Rf compares how far the spot moved with how far the solvent moved".
3. First step: give the reagent or the first measurement and ask what it shows.
4. Worked step: show the first observation matched to its meaning and ask for the next one.
5. Full step: show the checked answer with its reason; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.analysis_cylinder_for_exact`: measures 25.0 cm³ with a measuring cylinder → "Which piece of glassware delivers one fixed volume to a tenth of a cm³?"
- `mc.chm.o.analysis_rinse_with_water`: rinses the burette with water before filling → "What happens to the concentration if a film of water mixes with the solution?"
- `mc.chm.o.analysis_mean_includes_rough`: averages the rough titre too → "Which titres agree within 0.10 cm³, and why leave out the quick first run?"
- `mc.chm.o.analysis_rf_inverted`: divides the solvent distance by the spot distance → "Can a spot move further than the solvent carrying it, so can Rf be above one?"
- `mc.chm.o.analysis_rf_from_paper_edge`: measures from the paper's edge → "Where did the spot start moving from: the paper's edge or the pencil line?"
- `mc.chm.o.analysis_filter_dissolved`: filters to remove dissolved salt → "Dissolved particles are tiny; do they stay on the filter paper or pass through?"
- `mc.chm.o.analysis_simple_for_miscible`: uses simple distillation for ethanol and water → "Ethanol boils at 78 °C and water at 100 °C, and both vapours rise together; which apparatus separates liquids this close?"
- `mc.chm.o.analysis_impurity_raises_mp`: thinks an impurity raises the melting point → "Salt is spread on icy roads; does it make ice melt at a higher or lower temperature?"
- `mc.chm.o.analysis_fe2_fe3_swapped`: matches green to iron(III) → "Rust is red-brown iron(III); which precipitate colour matches iron(III)?"
- `mc.chm.o.analysis_excess_ignored`: names zinc or aluminium from the first white precipitate only → "Several ions give a white precipitate; what happened when more reagent was added?"
- `mc.chm.o.analysis_halide_colours_mixed`: calls silver iodide white → "Chloride, bromide and iodide get darker down the group; which one gives yellow?"
- `mc.chm.o.analysis_splints_swapped`: uses a glowing splint for hydrogen → "Which gas helps things burn and so relights a glowing splint?"

## Confidence and escalation

- Two wrong ion identifications: switch to a table with columns for reagent, first result and result in excess.
- Three failures on separation: detour to elements, compounds and mixtures, then return.
- An Rf above one or below zero: treat it as an inverted or misplaced distance, not a slip.
- A question about the school's own apparatus or safety rules: save it for the teacher.

## Worked exchange

> Tutor: On a chromatogram the start line is 1.5 cm above the bottom edge. A spot has moved 3.6 cm above the start line and the solvent front is 8.0 cm above it. What is the Rf value?
> Student: 8.0 ÷ 3.6, so about 2.2.
> Tutor: Can a spot move further than the solvent that carries it? Which moved further here?
> Student: The solvent did. So it is 3.6 ÷ 8.0.
> Tutor: Yes. Work it out.
> Student: 0.45.
> Tutor: Correct (checked with the maths tool). An Rf value always lies between zero and one.

## Verification checklist

- Every Rf or mean titre shown has a check from math_evaluate.
- Both chromatogram distances run from the start line, not the paper's edge.
- A mean titre uses only titres that agree within 0.10 cm³.
- An ion is named only when every observation given fits it, including the result in excess.

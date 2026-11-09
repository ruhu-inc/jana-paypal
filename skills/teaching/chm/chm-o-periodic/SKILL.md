---
name: chm-o-periodic
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach the periodic table's groups, periods and trends from electron arrangement
description: "periodic table, groups and periods, alkali metals, halogens, transition elements, noble gases: read group and period from the electron arrangement, then predict trends and reactions. Not for: writing electron arrangements (chm-o-atoms) or the reactivity series of metals (chm-o-metals)."
subject: chemistry
levels: [O]
topics: [periodic table, groups and periods, group i, alkali metals, group vii, halogens, transition elements, noble gases]
kcs:
  - kc.chm.o.periodic.arrangement
  - kc.chm.o.periodic.group_i
  - kc.chm.o.periodic.group_vii
  - kc.chm.o.periodic.transition_elements
  - kc.chm.o.periodic.noble_gases
curriculum_refs: [CHM-O-08.01, CHM-O-08.02, CHM-O-08.03, CHM-O-08.04, CHM-O-08.05]
prerequisites: [kc.chm.o.atoms.electron_arrangement]
related: [chm-o-atoms, chm-o-metals]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: []
visuals: [periodic_table, electron_shells]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: temperature
    unit: degC
    min: -270
    max: 2000
    context: melting and boiling points predicted from trends in Group I, Group VII, the noble gases and transition metals
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student reads the group and the period of an element from its electron arrangement
  - The student predicts how a Group I metal reacts with water and how its reactivity changes down the group
  - The student predicts a halogen displacement and the trend in colour, state and reactivity down Group VII
  - The student contrasts transition elements with Group I metals and explains why noble gases are unreactive
---

# The periodic table

## When to use

Use when a question asks for the group or period of an element, or links them to its electrons.
Use it for trends and reactions down Group I and Group VII, including halogen displacement.
Use it for typical properties of transition elements, and for the unreactivity and uses of the noble gases.

Not for: writing electron arrangements or drawing shells (chm-o-atoms) or the full reactivity series of metals (chm-o-metals).

## What this skill does not do

- It does not explain trends with ionisation energies or electronegativity values.
- It does not cover Groups II to VI beyond reading their group number.
- It does not give a predicted trend before the student has tried.

## Objectives

1. The student reads the group and the period of an element from its electron arrangement.
2. The student predicts how a Group I metal reacts with water and how its reactivity changes down the group.
3. The student predicts a halogen displacement and the trend in colour, state and reactivity down Group VII.
4. The student contrasts transition elements with Group I metals and explains why noble gases are unreactive.

## Prerequisite probe

- "What is the electron arrangement of sodium, atomic number 11?" Expected: 2,8,1.

If it fails, detour to electron arrangement in chm-o-atoms, then return.

## Teaching sequence

1. Ask the student to write the electron arrangement, or to read it from the question. Done when the arrangement is on the board.
2. Ask how many shells hold electrons and how many electrons are in the outer shell. Done when the student gives the period from the shells and the group from the outer electrons.
3. For Group I, ask where the outer electron sits as atoms get bigger and how easily it is lost. Done when the student links a lower position to greater reactivity and names the hydroxide and hydrogen.
4. For Group VII, ask how easily a bigger atom gains an electron, then which halogen in the question is higher. Done when the student predicts whether displacement happens and names the products.
5. For transition elements or noble gases, ask for one property to compare and its reason. Done when the student gives the property with a reason from structure or electrons.
6. For a predicted value such as a melting point, ask the student to place it between its neighbours in the data given. Done when the value lies between the neighbours' values.

## Hint ladder

1. Orient: "Which element is this, and where is it in the table?"
2. Concept: state the rule, such as "the group number is the number of outer electrons; the period is the number of shells".
3. First step: write the electron arrangement and ask the student to count the outer electrons.
4. Worked step: show the group and period, or the trend's direction, and ask for the prediction.
5. Full step: show the answer with its reason; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.periodic_group_period_swapped`: gives the shell count as the group → "Which number counts the rows going down the table, and which counts the electrons in the outer shell?"
- `mc.chm.o.periodic_group_from_full_shell`: reads group VIII from 2,8,5 → "Which shell takes part in reactions: the full inner one or the outer one?"
- `mc.chm.o.periodic_group1_reactivity_decreases`: says lithium is more reactive than potassium → "Lithium fizzes gently in water and potassium bursts into a lilac flame; which is more reactive?"
- `mc.chm.o.periodic_group1_water_gives_oxygen`: names oxygen as the gas from sodium and water → "A lighted splint held to the gas gives a squeaky pop; which gas is that?"
- `mc.chm.o.periodic_group1_solution_acidic`: thinks the solution left is acidic → "Sodium and water make sodium hydroxide; what colour would that turn red litmus?"
- `mc.chm.o.periodic_halogen_reactivity_increases`: predicts iodine displaces chlorine → "Which atom pulls an extra electron in more strongly, the small chlorine atom or the large iodine atom?"
- `mc.chm.o.periodic_displacement_reversed`: says chlorine is pushed out of potassium bromide → "Chlorine is more reactive than bromine; which one ends up joined to potassium, and which one is set free?"
- `mc.chm.o.periodic_halogen_state_trend_reversed`: calls iodine a pale gas → "Chlorine is a gas and bromine a liquid at room temperature; what does that suggest for iodine below them?"
- `mc.chm.o.periodic_transition_like_group1`: expects iron to be soft and to melt easily → "Iron is used for bridges and furnaces; would a soft, low-melting metal do that job?"
- `mc.chm.o.periodic_transition_one_charge`: ignores the numbers in iron(II) and iron(III) → "Iron(II) sulfate is pale green and iron(III) compounds are orange-brown; can both contain the same iron ion?"
- `mc.chm.o.periodic_helium_eight`: gives helium eight outer electrons → "Helium has only two electrons in all; how many can be in its outer shell?"
- `mc.chm.o.periodic_noble_unreactive_because_gas`: says argon is unreactive because it is a gas → "Chlorine is a gas too and very reactive; so what makes argon different?"

## Confidence and escalation

- Two wrong group or period answers: switch to drawing the shells and counting them aloud.
- Three failures on reading the table: detour to electron arrangement, then return.
- A displacement prediction backwards: treat it as the displacement misconception, not a guess.
- A question about local uses of these elements in industry: save it for the teacher.

## Worked exchange

> Tutor: An element has the electron arrangement 2,8,7. Which group and which period is it in?
> Student: Group 3 and period 7.
> Tutor: Which number tells you how many shells hold electrons, and which tells you the outer electrons?
> Student: There are three shells, so period 3. There are seven outer electrons, so Group VII. It is chlorine.
> Tutor: Good. Will this element displace iodine from potassium iodide solution?
> Student: Yes. Chlorine is above iodine, so it is more reactive. The solution turns brown as iodine forms.
> Tutor: Correct, and the other product is potassium chloride.

## Verification checklist

- The group comes from the outer electrons and the period from the number of shells.
- A Group I trend says reactivity rises down the group; a Group VII trend says it falls.
- A displacement prediction names the more reactive halogen as the one that does the displacing.
- Any value given for a trend lies between the values of its neighbours in the data.

---
name: chm-o-atoms
kind: teaching
version: "0.1"
status: draft
language: en
summary: Teach atomic structure, isotopes, ions and the three kinds of bonding
description: "Atoms, isotopes, electron arrangement, ions, bonding, giant structures: count the particles, fill the shells, weight the isotopes, then link bonding to melting point and conductivity. Not for: formulae and moles (chm-o-stoichiometry) or periodic trends (chm-o-periodic)."
subject: chemistry
levels: [O]
topics: [atoms, isotopes, electron arrangement, ions, bonding, giant structures]
kcs:
  - kc.chm.o.atoms.element_compound_mixture
  - kc.chm.o.atoms.subatomic_particles
  - kc.chm.o.atoms.electron_arrangement
  - kc.chm.o.atoms.isotopes
  - kc.chm.o.atoms.ion_formation
  - kc.chm.o.atoms.covalent_bonding
  - kc.chm.o.atoms.structure_and_properties
  - kc.chm.o.atoms.giant_covalent
  - kc.chm.o.atoms.metallic_bonding
curriculum_refs: [CHM-O-02.01, CHM-O-02.02, CHM-O-02.03, CHM-O-02.04, CHM-O-02.05, CHM-O-02.06, CHM-O-02.07]
prerequisites:
  - kc.chm.o.states.particle_model
related: [chm-o-stoichiometry, chm-o-periodic, chm-o-states]
pedagogy: [pedagogy-guided-solve]
audience: student
roles: [coach, diagnoser, solver, checker]
requires_tools: [math_evaluate]
visuals: [electron_shells, particle_diagram, periodic_table]
answer_policy:
  mode: withhold_until_earned
  min_attempts: 1
escalation:
  after_failed_attempts: 3
  action: prerequisite_detour
plausibility:
  - quantity: relative_atomic_mass
    unit: dimensionless
    min: 1
    max: 240
    context: weighted mean masses of isotopes of real elements
  - quantity: abundance
    unit: percent
    min: 0
    max: 100
    context: percentage abundance of one isotope in a natural sample
  - quantity: particle_count
    unit: dimensionless
    min: 0
    max: 150
    context: protons, neutrons or electrons in one atom or simple ion
  - quantity: temperature
    unit: degC
    min: -273
    max: 3700
    context: melting and boiling points used to compare structures
owner: chemistry-lead
last_reviewed_by: not-yet-reviewed
last_reviewed_at: "2026-10-13"
success_criteria:
  - The student finds the protons, neutrons and electrons of an atom or ion and writes its electron arrangement
  - The student calculates a relative atomic mass as a weighted mean of isotope masses
  - The student predicts the bonding and ion charges from the elements present and their outer electrons
  - The student explains melting point and conductivity from the particles present and the forces between them
---

# Atoms, ions and bonding

## When to use

Use when a question asks for the particles in an atom or ion, an electron arrangement, a relative atomic mass from
isotope data, the charge on an ion, the shared pairs in a molecule, or why a substance melts high or low and
conducts or not. Also use to sort substances into elements, compounds and mixtures.

Not for: writing formulae or mole calculations (chm-o-stoichiometry), or trends down a group (chm-o-periodic).

## What this skill does not do

- It does not go beyond the first twenty elements for electron arrangements.
- It does not use orbitals, hybridisation or electronegativity values.
- It does not name metals or non-metals from memory alone; the periodic table decides.

## Objectives

1. The student finds the protons, neutrons and electrons of an atom or ion and writes its electron arrangement.
2. The student calculates a relative atomic mass as a weighted mean of isotope masses.
3. The student predicts the bonding and ion charges from the elements present and their outer electrons.
4. The student explains melting point and conductivity from the particles present and the forces between them.

## Prerequisite probe

- "How are particles arranged and how do they move in a solid?" Expected: close together in a regular pattern, vibrating about fixed positions.

If it fails, detour to the particle model (chm-o-states), then return.

## Teaching sequence

1. Ask which particles the question is about: an atom, an ion, a molecule or a giant structure. Done when the student names the kind of particle.
2. For counts, ask for protons from the proton number, neutrons as nucleon number minus proton number, and electrons as protons minus the ion's charge; the math_evaluate result is the reference value. Done when all three counts match the checked values.
3. For an arrangement, fill shells of 2, 8 and 8 from the proton number, then start a fourth. Done when the shell totals add to the electron count (checked with math_evaluate).
4. For isotopes, ask for the sum of each mass times its abundance, divided by 100; the math_evaluate result is the reference value. Done when the student's value matches and lies between the isotope masses.
5. For bonding, ask whether the elements are metal with non-metal (ionic), non-metals only (covalent) or one metal (metallic); outer electrons give the ion charge or the shared pairs. Done when the bond type and charge or pair count are stated with a reason.
6. For properties, ask what must be overcome on melting and what moves to carry charge. Done when the student names the force broken and the mobile charged particle, or says none is mobile.

## Hint ladder

1. Orient: "Is the question about one atom, an ion, a molecule or a whole structure?"
2. Concept: name the rule, such as "neutrons are the nucleon number minus the proton number".
3. First step: write the rule with symbols and ask the student to put the numbers in.
4. Worked step: show the substitution or the first shell and ask for the rest.
5. Full step: show the checked answer with its reason; the step counts as not mastered and a similar item is scheduled.

## Misconceptions

- `mc.chm.o.diatomic_as_compound`: calls O₂ a compound → "How many different elements are joined in an O₂ molecule?"
- `mc.chm.o.neutrons_nucleon_number`: gives the nucleon number as the neutrons → "Which particles does the nucleon number count, besides neutrons?"
- `mc.chm.o.ion_electrons_unchanged`: gives an ion the atom's electron count → "What does the charge on the ion tell you happened to its electrons?"
- `mc.chm.o.third_shell_overfilled`: puts electrons 19 and 20 in the third shell → "For the first twenty elements, how many electrons does the third shell hold before the fourth starts?"
- `mc.chm.o.isotope_simple_average`: averages isotope masses without abundances → "If one isotope makes up three quarters of the sample, should both masses count equally?"
- `mc.chm.o.ion_wrong_direction`: has a metal gain electrons → "Is it easier for sodium to lose one electron or to gain seven?"
- `mc.chm.o.covalent_multiple_bond_missed`: counts a double bond as one pair → "How many lines join the two atoms in O=O, and how many electrons does each line stand for?"
- `mc.chm.o.melting_breaks_covalent_bonds`: says water boils when its O–H bonds break → "When water boils, is the steam still H₂O?"
- `mc.chm.o.ionic_conduction_by_electrons`: says molten salt conducts by electrons → "What charged particles are in molten sodium chloride, and can they move?"
- `mc.chm.o.graphite_soft_bonds_weak`: says graphite is soft because its covalent bonds are weak → "If graphite's covalent bonds were weak, why is its melting point so high?"
- `mc.chm.o.metal_conducts_by_ions`: says metals conduct by moving ions → "In a solid wire, which particles are free to drift: the positive ions or the delocalised electrons?"

## Confidence and escalation

- Two wrong shell arrangements: switch to an electron shell diagram and fill it one electron at a time.
- Repeated mixing of bonding types: return to sorting the elements into metals and non-metals on the periodic table.
- Three failures on the particle picture: detour to the particle model, then return.

## Worked exchange

> Tutor: Chlorine has two isotopes: chlorine-35 makes up 75% of atoms and chlorine-37 makes up 25%. What is its relative atomic mass?
> Student: (35 + 37) ÷ 2, so 36.
> Tutor: Are the two isotopes equally common?
> Student: No. Chlorine-35 is three times as common, so it should count more.
> Tutor: Multiply each mass by its percentage, add, then divide by the total of the percentages.
> Student: (35 × 75 + 37 × 25) ÷ 100 = 35.5.
> Tutor: Correct (checked with the maths tool), and 35.5 sits nearer 35, as it should.

## Verification checklist

- Every count and mass was checked with math_evaluate.
- Electrons in an ion equal protons minus the charge, with the sign taken into account.
- Shell totals add to the electron count, with no shell above 2, 8 or 8 before the next starts.
- A relative atomic mass lies between the lightest and heaviest isotope masses.
- A property explanation names the particles present and the force overcome.
